use std::collections::HashMap;
use std::time::Duration;

use api::{
    Collection, ItemCounts, ItemQuery, ItemSort, ListedItem, Tag, TextItemType, UploadCandidate,
    UploadType,
};
use base64::prelude::{BASE64_STANDARD, Engine as _};
use dioxus::html::{FileData, HasFileData};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::auth_session::ItemLabels;
use crate::collections::{COLLECTIONS_CSS, CollectionChip, CollectionPicker, toggled};
use crate::confirm::ConfirmDialog;
use crate::date_filter::{DateFilter, DateSelection};
use crate::duplicates::{Duplicate, DuplicateDialog, DuplicateFile, duplicate_positions};
use crate::filters::{
    FavoritesToggle, FiltersMenu, SortMenu, TAG_LIST_LIMIT, TypeFilter, TypeTabs,
};
use crate::i18n::api_error_message;
use crate::icons::{
    IconArrowUp, IconClose, IconFile, IconLogout, IconPaperclip, IconSearch, IconStash, IconUser,
};
use crate::items::{ItemGrid, ItemViewer, TagPicker, TextTypeSelect, suggested_tags};
use crate::routes::Route;
use crate::selection::{
    SelectionBar, SelectionEvent, merged, toggled_id, use_selection_events, with_local_edits,
};
use crate::settings::AccountSettings;
use crate::text_kind::{TextKind, text_kind};
use crate::toast::{ToastHost, use_toasts};

const FILE_UPLOAD_INPUT_ID: &str = "home-file-upload-input";
/// The capture box: files pasted while it has focus are staged.
const CAPTURE_FORM_ID: &str = "home-capture-form";
/// The search box, focused by the Ctrl+F / ⌘F shortcut.
const SEARCH_INPUT_ID: &str = "home-search-input";
const LOGO_PNG: Asset = asset!("/assets/stash-logo.png");
/// How long typing must pause before a search request is sent.
const SEARCH_DEBOUNCE: Duration = Duration::from_millis(250);
const SEARCH_LIMIT: u32 = 50;

/// Image content types by file extension; any other file is uploaded as a
/// generic file item.
fn image_content_type(file_name: &str) -> Option<&'static str> {
    let lower = file_name.to_ascii_lowercase();
    if lower.ends_with(".png") {
        Some("image/png")
    } else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        Some("image/jpeg")
    } else if lower.ends_with(".gif") {
        Some("image/gif")
    } else if lower.ends_with(".webp") {
        Some("image/webp")
    } else {
        None
    }
}

/// An image's actual format, from its leading bytes. The upload is
/// authorized for the declared type and the backend checks the content
/// matches, so e.g. a PNG saved as ".jpg" must be declared as a PNG.
fn sniffed_image_content_type(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if data.starts_with(b"\xff\xd8\xff") {
        Some("image/jpeg")
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

/// Items waiting for the user to confirm their deletion: one, from its
/// menu, or the `selection` (which is left once they're deleted).
#[derive(Clone, PartialEq)]
struct PendingDelete {
    ids: Vec<String>,
    selection: bool,
}

/// A file the user has picked or dropped but not yet sent.
///
/// Whether it's an image is decided by the extension (Dioxus's
/// cross-platform `FileData` only exposes the name, not `File.type`); an
/// image's content type comes from its bytes where recognizable. For
/// non-images it's just `application/octet-stream`: the backend determines
/// the real type itself.
#[derive(Clone, PartialEq)]
struct PendingFile {
    file_name: String,
    content_type: &'static str,
    data: Vec<u8>,
    /// `data:` URL for an image's preview thumbnail (rather than an object
    /// URL, so it works on every platform `ui` supports); None for a
    /// non-image file, which is shown as an icon instead.
    preview_url: Option<String>,
}

/// Files the user sent that include duplicates (of saved items, or of an
/// earlier file among them), held while `DuplicateDialog` asks what to do
/// with those; with the caption and labels they were sent with.
#[derive(Clone, PartialEq)]
struct DuplicateReview {
    files: Vec<PendingFile>,
    /// Positions in `files` of the duplicates, in order, with what each
    /// duplicates.
    duplicates: Vec<(usize, Duplicate)>,
    caption: Option<String>,
    labels: ItemLabels,
}

impl PendingFile {
    fn is_image(&self) -> bool {
        self.preview_url.is_some()
    }

    /// What the duplicate check compares: the type, name and size it will be
    /// uploaded with. Never the content, so it costs nothing however large
    /// the file is.
    fn candidate(&self) -> UploadCandidate {
        UploadCandidate {
            upload_type: if self.is_image() {
                UploadType::Image
            } else {
                UploadType::File
            },
            file_name: self.file_name.clone(),
            size_bytes: self.data.len() as u64,
        }
    }
}

/// Reads `file` into a `PendingFile` ready for preview, without uploading
/// it. Shared by both the file-picker input and drag-and-drop, which differ
/// only in how they obtain the `FileData`.
async fn stage_file(file: FileData) -> Option<PendingFile> {
    let file_name = file.name();
    let data = file.read_bytes().await.ok()?.to_vec();

    let (content_type, preview_url) = match image_content_type(&file_name) {
        Some(guessed) => {
            let content_type = sniffed_image_content_type(&data).unwrap_or(guessed);
            (
                content_type,
                Some(format!(
                    "data:{content_type};base64,{}",
                    BASE64_STANDARD.encode(&data)
                )),
            )
        }
        None => ("application/octet-stream", None),
    };

    Some(PendingFile {
        file_name,
        content_type,
        data,
        preview_url,
    })
}

const HOME_CSS: Asset = asset!("/assets/styling/home.css");
/// Tag chips and the tag picker, used in the capture box.
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

#[component]
pub fn Home() -> Element {
    let session = use_context::<AuthSession>();
    let nav = use_navigator();
    // Messages from anything on the page (e.g. a file that couldn't open).
    use_toasts();

    {
        let session = session.clone();
        use_effect(move || {
            if !session.is_authenticated() {
                nav.push(Route::Auth {});
            }
        });
    }

    // Type, favorites, date, collection and tag filters. Applied by the
    // server, to the list and to search alike; changing any refetches
    // whichever is showing.
    let active_type = use_signal(|| TypeFilter::All);
    let mut selected_tags = use_signal(Vec::<Tag>::new);
    let mut selected_collections = use_signal(Vec::<Collection>::new);
    let favorites_only = use_signal(|| false);
    let saved_on = use_signal(|| None::<DateSelection>);
    let current_filters = move || {
        let (created_from, created_before) = saved_on().map(DateSelection::api_range).unzip();
        ItemQuery {
            item_type: active_type().api_type().map(str::to_string),
            kinds: active_type()
                .api_kinds()
                .into_iter()
                .map(str::to_string)
                .collect(),
            tag_ids: selected_tags().iter().map(|tag| tag.id.clone()).collect(),
            collection_ids: selected_collections()
                .iter()
                .map(|collection| collection.id.clone())
                .collect(),
            favorites_only: favorites_only(),
            created_from,
            created_before,
        }
    };

    // Listing order; search ignores it (ranked by relevance). `shuffle`
    // counts "Random" re-picks, each of which fetches a fresh shuffle.
    let sort = use_signal(ItemSort::default);
    let mut shuffle = use_signal(|| 0u32);

    // Re-runs whenever the filters or order change (they're read below,
    // outside the async block, which is what subscribes to them). `submit`, deletes and
    // tag edits call `.restart()` so changes show up without a reload. No
    // pagination yet. Resolves to the order it was fetched in too, as the
    // previous result stays on screen while a new order loads.
    let mut saved_items = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            let filters = current_filters();
            let sort = sort();
            shuffle();
            async move { (sort, session.list_items(None, 30, filters, sort).await) }
        }
    });

    // Item counts for the type tabs and favorites toggle. Unfiltered, so
    // they don't depend on the filters; restarted wherever items are
    // added, deleted, edited (a note can become a link) or (un)favorited.
    let mut item_counts = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            async move { session.count_items().await }
        }
    });

    // Search-as-you-type. `use_resource` re-runs whenever `search_query`
    // changes and drops the previous, still-running future — so the delay
    // below doubles as a debounce: only a pause in typing reaches the
    // server, and a slow response for an outdated query can never overwrite
    // a newer one. Resolves to `None` when the box is empty (show the
    // regular list instead).
    let mut search_query = use_signal(String::new);
    let mut search_results = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            let query = search_query().trim().to_string();
            let filters = current_filters();
            async move {
                if query.is_empty() {
                    return None;
                }
                Delay::new(SEARCH_DEBOUNCE).await;
                let result = session
                    .search_items(query.clone(), SEARCH_LIMIT, filters)
                    .await;
                Some((query, result))
            }
        }
    });

    // Items changed here but not refetched (a card's favorite, a bulk
    // change to the selection), as they now are, by id: shown in place of
    // the fetched ones until the next fetch arrives, which has the change
    // too. Keeps a card from showing its old state meanwhile, and the
    // selection's bulk actions working from the current one.
    let mut local_edits = use_signal(HashMap::<String, ListedItem>::new);
    use_effect(move || {
        let _ = saved_items.read();
        let _ = search_results.read();
        if !local_edits.peek().is_empty() {
            local_edits.write().clear();
        }
    });
    // The items on screen as fetched: the list, or the search results while
    // searching. None while loading, or if that failed.
    let fetched_items = move || -> Option<Vec<ListedItem>> {
        if search_query().trim().is_empty() {
            match &*saved_items.read() {
                Some((_, Ok(response))) => Some(response.items.clone()),
                _ => None,
            }
        } else {
            match &*search_results.read() {
                Some(Some((_, Ok(response)))) => Some(response.items.clone()),
                _ => None,
            }
        }
    };
    let shown_items =
        move || fetched_items().map(|items| with_local_edits(&items, &local_edits.read()));

    // The ids of the selected items; any selected is selection mode (see
    // `selection.rs`). A rectangle dragged over the cards selects the ones
    // it covers, added to `marquee_base`: nothing, or the selection when it
    // started with Shift/Ctrl/⌘ held. Escape clears the selection.
    let mut selection = use_signal(Vec::<String>::new);
    let mut marquee_base = use_signal(Vec::<String>::new);
    use_selection_events(move |event| match event {
        SelectionEvent::MarqueeStarted { additive } => {
            let base = if additive {
                selection.peek().clone()
            } else {
                Vec::new()
            };
            marquee_base.set(base);
        }
        SelectionEvent::MarqueeCovers(ids) => {
            let ids = merged(&marquee_base.peek(), &ids);
            selection.set(ids);
        }
        SelectionEvent::Escape => {
            if !selection.peek().is_empty() {
                selection.set(Vec::new());
            }
        }
    });
    let toggle_selected = use_callback(move |item_id: String| {
        let ids = toggled_id(&selection.peek(), &item_id);
        selection.set(ids);
    });
    // Items no longer shown (deleted, or changed so they no longer match
    // the filters) leave the selection.
    use_effect(move || {
        let Some(items) = fetched_items() else {
            return;
        };
        let shown = |id: &String| items.iter().any(|item| item.id == *id);
        if !selection.peek().iter().all(shown) {
            selection.write().retain(shown);
        }
    });

    let mut note = use_signal(String::new);
    // The type chosen for a note mixing text and URLs (see `text_kind`);
    // a bare URL is always a link and text without URLs always a note, so
    // the choice is only offered, and sent, for mixed text.
    let mut note_type = use_signal(|| TextItemType::Text);
    let mut is_submitting = use_signal(|| false);
    let mut status = use_signal(|| None::<String>);
    // Counts nested dragenter/dragleave pairs rather than a bool: the
    // pointer crossing from the container into a child element fires
    // dragleave-then-dragenter for that child, and only the count (not a
    // single flag) tells the container it's still being dragged over.
    let mut drag_depth = use_signal(|| 0i32);
    let mut pending_files = use_signal(Vec::<PendingFile>::new);
    // Tag names to put on whatever is sent next (the note, or every staged
    // file), and whether the tag picker is open.
    let mut pending_tags = use_signal(Vec::<String>::new);
    let mut picking_tag = use_signal(|| false);
    // Likewise the names of the collections to put it in (all of the staged
    // files, when several are sent at once), and whether their picker is
    // open.
    let mut pending_collections = use_signal(Vec::<String>::new);
    let mut picking_collection = use_signal(|| false);
    // The user's suggested tags (recently, then frequently used), minus any
    // already added. Refetched as pending tags change (including being
    // cleared once something is saved with them) and when a card's tags
    // change. Empty — so the line isn't shown — until the user has tags.
    let mut suggested = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            let exclude = pending_tags();
            async move { suggested_tags(&session, None, &exclude).await }
        }
    });

    // "Surprise me": one of the user's items, picked at random by the
    // server and open in its own view until closed.
    let mut surprise = use_signal(|| None::<ListedItem>);

    // Collections and tags go away with their last item (deleted, or
    // edited out of them), so after either, drops any gone from the active
    // filters, which would otherwise keep a chip for something that no
    // longer exists. Looked up by name (the lists are searched, not
    // fetched whole), and written only if something went.
    let prune_filters = use_callback({
        let session = session.clone();
        move |()| {
            let collections = selected_collections();
            let tags = selected_tags();
            if collections.is_empty() && tags.is_empty() {
                return;
            }
            let session = session.clone();
            spawn(async move {
                let mut kept_collections = Vec::new();
                for collection in &collections {
                    match session
                        .list_collections(collection.name.clone(), TAG_LIST_LIMIT)
                        .await
                    {
                        Ok(found) if !found.iter().any(|c| c.id == collection.id) => {}
                        // Kept on errors: better a stale chip than a lost filter.
                        _ => kept_collections.push(collection.clone()),
                    }
                }
                let mut kept_tags = Vec::new();
                for tag in &tags {
                    match session.list_tags(tag.name.clone(), TAG_LIST_LIMIT).await {
                        Ok(found) if !found.iter().any(|t| t.id == tag.id) => {}
                        _ => kept_tags.push(tag.clone()),
                    }
                }
                if kept_collections.len() < collections.len() {
                    selected_collections
                        .write()
                        .retain(|c| kept_collections.iter().any(|kept| kept.id == c.id));
                }
                if kept_tags.len() < tags.len() {
                    selected_tags
                        .write()
                        .retain(|t| kept_tags.iter().any(|kept| kept.id == t.id));
                }
            });
        }
    });

    // Deleting always asks first (see `ConfirmDialog`): Delete in a card's
    // or an open item's menu asks about that item, and in selection mode
    // about the selection. Confirmed, they're deleted one by one; then
    // whichever view is showing (list and search) is refetched, so the
    // cards disappear from both, and the suggested tags, since an item may
    // have been a tag's only use. The dialog stays open with the error, and
    // the items that are left, if any couldn't be deleted.
    let mut pending_delete = use_signal(|| None::<PendingDelete>);
    let mut deleting = use_signal(|| false);
    let mut delete_error = use_signal(|| None::<String>);
    let delete_item = use_callback(move |item_id: String| {
        delete_error.set(None);
        pending_delete.set(Some(PendingDelete {
            ids: vec![item_id],
            selection: false,
        }));
    });
    let delete_selected = use_callback(move |()| {
        delete_error.set(None);
        pending_delete.set(Some(PendingDelete {
            ids: selection(),
            selection: true,
        }));
    });
    let confirm_delete = use_callback({
        let session = session.clone();
        move |()| {
            let Some(pending) = pending_delete() else {
                return;
            };
            if deleting() {
                return;
            }
            let session = session.clone();
            spawn(async move {
                deleting.set(true);
                let mut deleted = Vec::new();
                let mut failed = Vec::new();
                let mut failure = None;
                for item_id in &pending.ids {
                    match session.delete_item(item_id.clone()).await {
                        Ok(()) => deleted.push(item_id.clone()),
                        Err(err) => {
                            failed.push(item_id.clone());
                            failure = Some(err);
                        }
                    }
                }
                if !deleted.is_empty() {
                    if surprise
                        .peek()
                        .as_ref()
                        .is_some_and(|item| deleted.contains(&item.id))
                    {
                        surprise.set(None);
                    }
                    selection.write().retain(|id| !deleted.contains(id));
                    saved_items.restart();
                    search_results.restart();
                    item_counts.restart();
                    suggested.restart();
                    prune_filters.call(());
                }
                deleting.set(false);
                match failure {
                    None => {
                        pending_delete.set(None);
                        if pending.selection {
                            selection.set(Vec::new());
                        }
                    }
                    Some(err) => {
                        let error = api_error_message(&err);
                        delete_error.set(Some(if pending.ids.len() == 1 {
                            t!("home-delete-failed", error: error)
                        } else {
                            t!("delete-failed-some", error: error)
                        }));
                        pending_delete.set(Some(PendingDelete {
                            ids: failed,
                            selection: pending.selection,
                        }));
                    }
                }
            });
        }
    });

    // A card's tags or content changed: refetch, so filters stay accurate
    // (an item that lost a filtered-by tag drops out, and an edited note
    // may have become a link or vice versa).
    let refresh_items = use_callback(move |()| {
        saved_items.restart();
        search_results.restart();
        item_counts.restart();
        suggested.restart();
        prune_filters.call(());
    });

    // A card's favorite state changed. The card already shows it, so only
    // refetch items when showing favorites only, where it may need to drop
    // out; the favorites count always changes. Kept as a local edit, so the
    // selection's Favorites action knows it.
    let favorite_changed = use_callback(move |(item_id, favorite): (String, bool)| {
        let item =
            shown_items().and_then(|items| items.into_iter().find(|item| item.id == item_id));
        if let Some(item) = item {
            local_edits.write().insert(
                item_id,
                ListedItem {
                    is_favorite: favorite,
                    ..item
                },
            );
        }
        item_counts.restart();
        if favorites_only() {
            saved_items.restart();
            search_results.restart();
        }
    });

    // A bulk action changed the selected items' tags or collections, or
    // their favorite state: shown right away, refetched like a card's
    // change.
    let selection_labels_changed = use_callback(move |items: Vec<ListedItem>| {
        local_edits
            .write()
            .extend(items.into_iter().map(|item| (item.id.clone(), item)));
        refresh_items.call(());
    });
    let selection_favorites_changed = use_callback(move |items: Vec<ListedItem>| {
        local_edits
            .write()
            .extend(items.into_iter().map(|item| (item.id.clone(), item)));
        item_counts.restart();
        if favorites_only() {
            saved_items.restart();
            search_results.restart();
        }
    });

    // A tag clicked on a card: add it to the tag filter, which refetches
    // whichever view is showing (list or search).
    let filter_by_tag = use_callback(move |tag: Tag| {
        if !selected_tags.read().iter().any(|t| t.id == tag.id) {
            selected_tags.write().push(tag);
        }
    });

    let surprise_me = use_callback({
        let session = session.clone();
        move |()| {
            let session = session.clone();
            spawn(async move {
                match session.random_item().await {
                    Ok(Some(item)) => surprise.set(Some(item)),
                    Ok(None) => status.set(Some(t!("surprise-me-nothing"))),
                    Err(err) => status.set(Some(t!(
                        "surprise-me-failed",
                        error: api_error_message(&err)
                    ))),
                }
            });
        }
    });

    // Uploads files (already checked for duplicates) with a caption and
    // labels, then refetches. Submitting until it's done. Skipped duplicates
    // aren't reported: skipping is what the user chose, not a failure.
    let mut duplicate_review = use_signal(|| None::<DuplicateReview>);
    let upload_files = use_callback({
        let session = session.clone();
        move |(files, caption, labels): (Vec<PendingFile>, Option<String>, ItemLabels)| {
            let session = session.clone();
            spawn(async move {
                is_submitting.set(true);
                // One failure shouldn't stop the rest from uploading;
                // report the last error, if any, once all are done.
                let mut last_error = None;
                for file in files {
                    let result = if file.is_image() {
                        session
                            .create_image_item(
                                &file.file_name,
                                file.content_type,
                                file.data,
                                caption.clone(),
                                labels.clone(),
                            )
                            .await
                    } else {
                        session
                            .create_file_item(
                                &file.file_name,
                                file.content_type,
                                file.data,
                                caption.clone(),
                                labels.clone(),
                            )
                            .await
                    };
                    if let Err(err) = result {
                        last_error = Some(api_error_message(&err));
                    }
                }
                // Keep the text, tags and collections if anything
                // failed, so they aren't lost.
                if last_error.is_none() {
                    note.set(String::new());
                    pending_tags.set(Vec::new());
                    pending_collections.set(Vec::new());
                }
                status.set(last_error);
                saved_items.restart();
                search_results.restart();
                item_counts.restart();

                is_submitting.set(false);
            });
        }
    });
    // The duplicate dialog's answer: for each duplicate, whether to upload
    // it anyway. The skipped ones are dropped, everything else uploads.
    let resolve_duplicates = move |upload: Vec<bool>| {
        let Some(review) = duplicate_review.write().take() else {
            return;
        };
        let skipped: Vec<usize> = review
            .duplicates
            .iter()
            .zip(&upload)
            .filter(|(_, upload)| !**upload)
            .map(|((position, _), _)| *position)
            .collect();
        let files = review
            .files
            .into_iter()
            .enumerate()
            .filter(|(position, _)| !skipped.contains(position))
            .map(|(_, file)| file)
            .collect();
        upload_files.call((files, review.caption, review.labels));
    };
    // Cancelled: nothing uploads, and every file is staged again.
    let cancel_duplicates = move |()| {
        if let Some(review) = duplicate_review.write().take() {
            pending_files.set(review.files);
        }
        is_submitting.set(false);
    };

    // A `Callback` (Copy) so both the form's submit and the text area's
    // Enter key can call it.
    let submit = {
        let session = session.clone();
        use_callback(move |()| {
            if is_submitting() {
                return;
            }

            // With files staged, the typed text is their caption: each file
            // becomes one item carrying both (image/file above, text
            // below), not a separate note.
            // Nothing is uploaded until it's known which files are already
            // saved (same content, whatever the name): if any are, the user
            // first decides, per file, whether to skip it or upload another
            // copy (see `DuplicateDialog`); the others upload as usual.
            let files = std::mem::take(&mut *pending_files.write());
            if !files.is_empty() {
                let session = session.clone();
                let caption = Some(note().trim().to_string()).filter(|text| !text.is_empty());
                let labels = ItemLabels {
                    tags: pending_tags(),
                    collections: pending_collections(),
                };
                spawn(async move {
                    is_submitting.set(true);
                    status.set(None);

                    let candidates: Vec<UploadCandidate> =
                        files.iter().map(PendingFile::candidate).collect();
                    let distinct: Vec<UploadCandidate> = candidates
                        .iter()
                        .enumerate()
                        .filter(|(position, file)| !candidates[..*position].contains(file))
                        .map(|(_, file)| file.clone())
                        .collect();
                    match session.find_duplicates(distinct).await {
                        Ok(groups) => {
                            let duplicates = duplicate_positions(&candidates, &groups);
                            if duplicates.is_empty() {
                                upload_files.call((files, caption, labels));
                            } else {
                                // Stays submitting (the capture box locked)
                                // until the dialog is answered.
                                duplicate_review.set(Some(DuplicateReview {
                                    files,
                                    duplicates,
                                    caption,
                                    labels,
                                }));
                            }
                        }
                        Err(err) => {
                            // Nothing was sent: the files stay staged.
                            pending_files.set(files);
                            status.set(Some(t!(
                                "duplicates-check-failed",
                                error: api_error_message(&err)
                            )));
                            is_submitting.set(false);
                        }
                    }
                });
                return;
            }

            if note().trim().is_empty() {
                return;
            }

            let session = session.clone();
            spawn(async move {
                is_submitting.set(true);
                status.set(None);

                let text = note().trim().to_string();
                let chosen_type = (text_kind(&text) == TextKind::Mixed).then_some(note_type());
                let labels = ItemLabels {
                    tags: pending_tags(),
                    collections: pending_collections(),
                };
                match session.create_text_item(&text, labels, chosen_type).await {
                    Ok(_) => {
                        note.set(String::new());
                        note_type.set(TextItemType::Text);
                        pending_tags.set(Vec::new());
                        pending_collections.set(Vec::new());
                        saved_items.restart();
                        search_results.restart();
                        item_counts.restart();
                    }
                    Err(err) => status.set(Some(api_error_message(&err))),
                }

                is_submitting.set(false);
            });
        })
    };

    let stage_picked_files = move |evt: FormEvent| async move {
        if is_submitting() {
            return;
        }
        let files = evt.files();
        if files.is_empty() {
            return;
        }
        status.set(None);
        for file in files {
            match stage_file(file).await {
                Some(file) => pending_files.write().push(file),
                None => status.set(Some(t!("home-read-failed"))),
            }
        }
    };

    let handle_drop = move |evt: DragEvent| {
        // Must run synchronously, before any `.await`, or the browser's
        // default action (opening the dropped file) fires first.
        evt.prevent_default();
        drag_depth.set(0);

        if is_submitting() {
            return;
        }
        let files = evt.files();
        if files.is_empty() {
            return;
        }
        spawn(async move {
            status.set(None);
            for file in files {
                match stage_file(file).await {
                    Some(file) => pending_files.write().push(file),
                    None => status.set(Some(t!("home-read-failed"))),
                }
            }
        });
    };

    let suggestions: Vec<String> = match &*suggested.read() {
        Some(Ok(tags)) => tags.iter().map(|tag| tag.name.clone()).collect(),
        _ => Vec::new(),
    };
    // Counts that failed to load are left out, like while loading.
    let counts: Option<ItemCounts> = match &*item_counts.read() {
        Some(Ok(counts)) => Some(*counts),
        _ => None,
    };

    // The avatar's letters; blank until the account has loaded (or if it
    // couldn't be).
    let current_user = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            async move { session.current_user().await }
        }
    });
    let initials = match &*current_user.read() {
        Some(Ok(user)) => email_initials(&user.email),
        _ => String::new(),
    };

    let selected: Vec<String> = selection();
    let selected_items: Vec<ListedItem> = shown_items()
        .unwrap_or_default()
        .into_iter()
        .filter(|item| selected.contains(&item.id))
        .collect();
    let edits = local_edits();

    // Ctrl+F / ⌘F focuses the search box. One document-level listener,
    // registered once per page load.
    use_effect(move || {
        let script = format!(
            r#"
                if (!window.__stashSearchShortcut) {{
                    window.__stashSearchShortcut = true;
                    document.addEventListener("keydown", (e) => {{
                        if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "f") {{
                            const input = document.getElementById("{SEARCH_INPUT_ID}");
                            if (input) {{
                                e.preventDefault();
                                input.focus();
                                input.select();
                            }}
                        }}
                    }});
                }}
                "#
        );
        document::eval(&script);
    });

    // Pasting files (a screenshot, an image copied from a page, files copied
    // in the file manager) into the capture box stages them, like picking
    // them. Dioxus's paste event carries no clipboard data, so a
    // document-level listener hands them to the file input instead and
    // fires its `change`: `stage_picked_files` then takes them as picked.
    // Pastes without files (text, links) are left alone. A pasted image
    // without a real name (a screenshot: browsers call every clipboard image
    // "image.png", or give none) is named after the moment it was pasted,
    // e.g. `image-2026-09-26_17_05_12.png`, local time; its extension comes
    // from its type, since staging recognizes images by extension.
    use_effect(move || {
        let script = format!(
            r##"
                if (!window.__stashPasteFiles) {{
                    window.__stashPasteFiles = true;
                    const pad = (n) => String(n).padStart(2, "0");
                    const pastedName = (file, now) => {{
                        const unnamed = !file.name || /^image\.[a-z0-9]+$/i.test(file.name);
                        if (!unnamed || !file.type.startsWith("image/")) {{
                            return file.name;
                        }}
                        const subtype = file.type.slice("image/".length).split(/[+;]/)[0];
                        const extension = {{ jpeg: "jpg" }}[subtype] ?? subtype;
                        const date = `${{now.getFullYear()}}-${{pad(now.getMonth() + 1)}}-${{pad(now.getDate())}}`;
                        const time = `${{pad(now.getHours())}}_${{pad(now.getMinutes())}}_${{pad(now.getSeconds())}}`;
                        return `image-${{date}}_${{time}}.${{extension}}`;
                    }};
                    document.addEventListener("paste", (e) => {{
                        const files = e.clipboardData?.files;
                        if (!files?.length || !e.target.closest?.("#{CAPTURE_FORM_ID}")) {{
                            return;
                        }}
                        const input = document.getElementById("{FILE_UPLOAD_INPUT_ID}");
                        if (!input || input.disabled) {{
                            return;
                        }}
                        e.preventDefault();
                        const transfer = new DataTransfer();
                        const now = new Date();
                        for (const file of files) {{
                            const name = pastedName(file, now);
                            transfer.items.add(
                                name === file.name
                                    ? file
                                    : new File([file], name, {{ type: file.type, lastModified: file.lastModified }})
                            );
                        }}
                        input.files = transfer.files;
                        input.dispatchEvent(new Event("change", {{ bubbles: true }}));
                    }});
                }}
                "##
        );
        document::eval(&script);
    });

    rsx! {
        document::Link { rel: "preconnect", href: "https://fonts.googleapis.com" }
        document::Link {
            rel: "stylesheet",
            href: "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap",
        }
        document::Link { rel: "stylesheet", href: HOME_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }
        document::Link { rel: "stylesheet", href: COLLECTIONS_CSS }

        ToastHost {}

        div {
            class: if drag_depth() > 0 { "home home-dragging" } else { "home" },
            ondragenter: move |evt| {
                evt.prevent_default();
                drag_depth += 1;
            },
            ondragleave: move |evt| {
                evt.prevent_default();
                drag_depth -= 1;
            },
            // Required for `ondrop` to fire at all — browsers reject drops
            // on elements that don't cancel dragover's default action.
            ondragover: move |evt| evt.prevent_default(),
            ondrop: handle_drop,

            TopBar {
                initials,
                // Unknown until the counts load: enabled meanwhile.
                can_surprise: counts.is_none_or(|counts| counts.types.total() > 0),
                on_surprise: surprise_me,
            }

            if let Some(item) = surprise() {
                ItemViewer {
                    key: "{item.id}",
                    item,
                    on_close: move |_| surprise.set(None),
                    on_delete: delete_item,
                    on_changed: refresh_items,
                    on_tag_click: filter_by_tag,
                }
            }

            div { class: "home-hero",
                h1 { class: "home-title", {t!("home-title")} }
                p { class: "home-tagline", {t!("home-tagline")} }

                if drag_depth() > 0 {
                    div { class: "home-drop-hint", {t!("home-drop-hint")} }
                }

                form {
                    id: CAPTURE_FORM_ID,
                    class: "home-input-wrap",
                    onsubmit: move |evt| {
                        evt.prevent_default();
                        submit.call(());
                    },
                    div { class: "home-input-inner",
                        // Staged images render inside the same card as the
                        // text row, not above or outside it — visually part
                        // of the message that Send is about to submit,
                        // rather than a floating block that leaves the
                        // input looking empty. Laid out 3-per-row so a
                        // large batch doesn't become one long scrolling
                        // line.
                        if !pending_files().is_empty() {
                            div { class: "home-image-preview-grid",
                                for (index , file) in pending_files().into_iter().enumerate() {
                                    div {
                                        class: "home-image-preview",
                                        key: "{index}-{file.file_name}",
                                        if let Some(preview_url) = &file.preview_url {
                                            img {
                                                class: "home-image-preview-thumb",
                                                src: "{preview_url}",
                                                alt: "{file.file_name}",
                                            }
                                        } else {
                                            span { class: "home-image-preview-thumb home-file-preview-icon",
                                                IconFile {}
                                            }
                                        }
                                        span { class: "home-image-preview-name", "{file.file_name}" }
                                        button {
                                            class: "home-image-preview-remove",
                                            r#type: "button",
                                            title: t!("home-remove-file"),
                                            disabled: is_submitting(),
                                            onclick: move |_| {
                                                pending_files.write().remove(index);
                                            },
                                            IconClose {}
                                        }
                                    }
                                }
                            }
                        }
                        // Collections and tags for what's sent next: chips
                        // (× removes) and, while picking, the tag picker.
                        // Kept (empty) while the collection picker is open,
                        // so the first chip doesn't push the picker down
                        // under the pointer.
                        if !pending_collections().is_empty() || !pending_tags().is_empty()
                            || picking_tag() || picking_collection()
                        {
                            div { class: "home-tags-row",
                                for name in pending_collections() {
                                    CollectionChip {
                                        key: "collection-{name}",
                                        name: name.clone(),
                                        disabled: is_submitting(),
                                        on_remove: move |_| {
                                            pending_collections.set(toggled(&pending_collections(), &name));
                                        },
                                    }
                                }
                                for (index , name) in pending_tags().into_iter().enumerate() {
                                    span { class: "tag-chip", key: "{name}", title: "{name}",
                                        span { class: "tag-chip-name", "{name}" }
                                        button {
                                            class: "tag-chip-remove",
                                            r#type: "button",
                                            title: t!("tags-remove"),
                                            aria_label: t!("tags-remove-named", name: name.as_str()),
                                            disabled: is_submitting(),
                                            onclick: move |_| {
                                                pending_tags.write().remove(index);
                                            },
                                            IconClose {}
                                        }
                                    }
                                }
                                if picking_tag() {
                                    TagPicker {
                                        exclude: pending_tags(),
                                        busy: is_submitting(),
                                        on_pick: move |name: String| {
                                            let duplicate = pending_tags()
                                                .iter()
                                                .any(|tag| tag.to_lowercase() == name.to_lowercase());
                                            if !duplicate {
                                                pending_tags.write().push(name);
                                            }
                                            picking_tag.set(false);
                                        },
                                        on_close: move |_| picking_tag.set(false),
                                    }
                                }
                            }
                        }
                        div { class: "home-input-row",
                            // Grows with its text up to a max height, then
                            // scrolls: a hidden mirror holds the same text,
                            // and the grid cell both share takes the
                            // mirror's wrapped height (a textarea can't
                            // size itself to its content in every browser).
                            div { class: "home-input-grow",
                                textarea {
                                    class: "home-input",
                                    rows: 1,
                                    placeholder: t!("home-input-placeholder"),
                                    value: "{note}",
                                    oninput: move |evt| note.set(evt.value()),
                                    // Enter sends; Shift+Enter is a new line.
                                    // Not while an IME is composing, where
                                    // Enter confirms the composition.
                                    onkeydown: move |evt| {
                                        if evt.key() == Key::Enter && !evt.modifiers().shift()
                                            && !evt.is_composing()
                                        {
                                            evt.prevent_default();
                                            submit.call(());
                                        }
                                    },
                                }
                                // Trailing space: a final newline would
                                // otherwise add no height.
                                div { class: "home-input-mirror", aria_hidden: "true", "{note} " }
                            }
                            input {
                                r#type: "file",
                                id: FILE_UPLOAD_INPUT_ID,
                                class: "home-image-input",
                                // No `accept` filter: any file can be saved.
                                // Images get the image pipeline; everything
                                // else is stored as a file item.
                                multiple: true,
                                disabled: is_submitting(),
                                onchange: stage_picked_files,
                            }
                            // With files staged the text is their caption,
                            // which has no type.
                            if pending_files().is_empty() && text_kind(&note()) == TextKind::Mixed {
                                TextTypeSelect {
                                    class: "home-input-type",
                                    value: note_type(),
                                    disabled: is_submitting(),
                                    on_change: move |value| note_type.set(value),
                                }
                            }
                            label {
                                class: "home-input-attach",
                                r#for: FILE_UPLOAD_INPUT_ID,
                                title: t!("home-attach"),
                                IconPaperclip {}
                            }
                            button {
                                class: "home-input-submit",
                                r#type: "submit",
                                disabled: is_submitting(),
                                span { {t!("home-submit")} }
                                IconArrowUp {}
                            }
                        }
                        // [ SUGGESTED: #tag • #tag … ]  [ + Add tag ] [ + Collection ]
                        div { class: "home-options-row",
                            // One click adds a suggestion to the pending
                            // tags; ones already added are left out. Empty
                            // while the user has no tags to suggest.
                            div { class: "home-suggested",
                                if !suggestions.is_empty() {
                                    span { class: "home-suggested-label", {t!("tags-suggested-label")} }
                                }
                                for (index , name) in suggestions.into_iter().enumerate() {
                                    if index > 0 {
                                        span { class: "home-suggested-sep", "•" }
                                    }
                                    button {
                                        class: "home-suggested-tag",
                                        r#type: "button",
                                        disabled: is_submitting(),
                                        onclick: {
                                            let name = name.clone();
                                            move |_| pending_tags.write().push(name.clone())
                                        },
                                        "#{name}"
                                    }
                                }
                            }
                            div { class: "home-options-actions",
                                button {
                                    class: "home-option-add",
                                    r#type: "button",
                                    title: t!("tags-add-title"),
                                    disabled: is_submitting() || picking_tag(),
                                    onclick: move |_| {
                                        picking_collection.set(false);
                                        picking_tag.set(true);
                                    },
                                    span { class: "home-option-plus", "+" }
                                    {t!("tags-add")}
                                }
                                div { class: "collection-picker-anchor",
                                    button {
                                        class: if pending_collections().is_empty() { "home-option-add" } else { "home-option-add home-option-add-active" },
                                        r#type: "button",
                                        title: t!("collections-add-title"),
                                        aria_haspopup: "dialog",
                                        aria_expanded: if picking_collection() { "true" } else { "false" },
                                        disabled: is_submitting(),
                                        onclick: move |_| {
                                            picking_tag.set(false);
                                            picking_collection.toggle();
                                        },
                                        span { class: "home-option-plus", "+" }
                                        {t!("collections-add")}
                                    }
                                    if picking_collection() {
                                        CollectionPicker {
                                            selected: pending_collections(),
                                            busy: is_submitting(),
                                            align_right: true,
                                            on_toggle: move |name: String| {
                                                pending_collections.set(toggled(&pending_collections(), &name));
                                            },
                                            on_close: move |_| picking_collection.set(false),
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                if let Some(message) = status() {
                    p { class: "home-status", "{message}" }
                }
            }

            // Full-width workspace band (a sibling of the narrow hero, not
            // nested in it) so it can span the whole page and grow to fill
            // the rest of the viewport, with only the content inside it
            // kept to a centered — but wider-than-the-hero — reading width.
            div { class: "stash-main",
                div { class: "stash-section",
                    // [ All | Notes | Media ▾ | Links | Files ▾ | ♥ ]  [ Date ▾ ] [ Search ] [ Filters ▾ ] [ ⇅ ]
                    // — typing in the search swaps the list below for
                    // semantic search results; the type, favorites, date,
                    // collection and tag filters apply to either. In
                    // selection mode, the selection's bulk actions instead.
                    div { class: "stash-controls",
                        if !selected.is_empty() {
                            SelectionBar {
                                items: selected_items,
                                on_cancel: move |_| selection.set(Vec::new()),
                                on_labels_changed: selection_labels_changed,
                                on_favorites_changed: selection_favorites_changed,
                                on_delete: delete_selected,
                            }
                        } else {
                            div { class: "stash-controls-group",
                                TypeTabs { value: active_type, counts }
                                div { class: "stash-controls-divider" }
                                FavoritesToggle { value: favorites_only, count: counts.map(|c| c.favorites) }
                            }
                            div { class: "stash-controls-query",
                                DateFilter { value: saved_on }
                                div { class: "stash-search-wrap",
                                    IconSearch {}
                                    input {
                                        id: SEARCH_INPUT_ID,
                                        class: "stash-search-input",
                                        r#type: "search",
                                        placeholder: t!("search-placeholder"),
                                        value: "{search_query}",
                                        oninput: move |evt| search_query.set(evt.value()),
                                    }
                                    if !search_query().is_empty() {
                                        button {
                                            class: "stash-search-clear",
                                            r#type: "button",
                                            title: t!("search-clear"),
                                            aria_label: t!("search-clear"),
                                            onclick: move |_| {
                                                search_query.set(String::new());
                                                // Keep the cursor in the box so the user can type a new query.
                                                document::eval(&format!(
                                                    r#"document.getElementById("{SEARCH_INPUT_ID}")?.focus();"#
                                                ));
                                            },
                                            IconClose {}
                                        }
                                    }
                                }
                                FiltersMenu { tags: selected_tags, collections: selected_collections }
                                SortMenu {
                                    value: sort,
                                    on_reshuffle: move |()| shuffle += 1,
                                    disabled: !search_query().trim().is_empty(),
                                }
                            }
                        }
                    }

                    if search_query().trim().is_empty() {
                        {match &*saved_items.read() {
                            None => rsx! {
                                div { class: "stash-empty",
                                    p { {t!("home-loading")} }
                                }
                            },
                            Some((_, Err(err))) => rsx! {
                                div { class: "stash-empty",
                                    p { {t!("home-load-failed", error: api_error_message(err))} }
                                }
                            },
                            Some((_, Ok(response))) if response.items.is_empty() && current_filters() == ItemQuery::default() => rsx! {
                                div { class: "stash-empty",
                                    IconStash {}
                                    p { {t!("home-empty")} }
                                }
                            },
                            Some((sort, Ok(response))) => item_results(
                                &with_local_edits(&response.items, &edits),
                                *sort != ItemSort::Random,
                                t!("home-no-filter-matches"),
                                selected.clone(),
                                toggle_selected,
                                delete_item,
                                refresh_items,
                                favorite_changed,
                                refresh_items,
                                filter_by_tag,
                            ),
                        }}
                    } else {
                        {match &*search_results.read() {
                            Some(Some((query, Ok(response)))) => item_results(
                                &with_local_edits(&response.items, &edits),
                                false,
                                t!("search-no-results", query: query.as_str()),
                                selected.clone(),
                                toggle_selected,
                                delete_item,
                                refresh_items,
                                favorite_changed,
                                refresh_items,
                                filter_by_tag,
                            ),
                            Some(Some((_, Err(err)))) => rsx! {
                                div { class: "stash-empty",
                                    p { {t!("search-failed", error: api_error_message(err))} }
                                }
                            },
                            // Debouncing, or the request is in flight.
                            _ => rsx! {
                                div { class: "stash-empty",
                                    p { {t!("search-searching")} }
                                }
                            },
                        }}
                    }
                }
            }

            if let Some(review) = duplicate_review() {
                DuplicateDialog {
                    files: review
                        .duplicates
                        .iter()
                        .map(|(position, duplicate)| DuplicateFile {
                            file_name: review.files[*position].file_name.clone(),
                            stored: duplicate.stored.clone(),
                            same_as_in_batch: duplicate
                                .first_in_batch
                                .map(|first| review.files[first].file_name.clone()),
                        })
                        .collect::<Vec<_>>(),
                    on_resolve: resolve_duplicates,
                    on_cancel: cancel_duplicates,
                }
            }

            if let Some(pending) = pending_delete() {
                ConfirmDialog {
                    title: t!("delete-confirm-title", count: pending.ids.len()),
                    message: t!("delete-confirm-message"),
                    confirm_label: t!("item-delete"),
                    busy_label: t!("delete-confirm-deleting"),
                    busy: deleting(),
                    error: delete_error(),
                    on_confirm: confirm_delete,
                    on_cancel: move |_| {
                        pending_delete.set(None);
                        delete_error.set(None);
                    },
                }
            }
        }
    }
}

/// Renders a list page or search results (already filtered by the
/// server), or `empty_message` if there are none. `group_by_day` for
/// chronological listings (see `ItemGrid`). `selected`: the selected
/// items' ids, any of which puts the grid in selection mode.
#[allow(clippy::too_many_arguments)]
fn item_results(
    items: &[ListedItem],
    group_by_day: bool,
    empty_message: String,
    selected: Vec<String>,
    on_toggle_selected: Callback<String>,
    on_delete: Callback<String>,
    on_tags_changed: Callback<()>,
    on_favorite_changed: Callback<(String, bool)>,
    on_edited: Callback<()>,
    on_tag_click: Callback<Tag>,
) -> Element {
    if items.is_empty() {
        rsx! {
            div { class: "stash-empty",
                p { "{empty_message}" }
            }
        }
    } else {
        rsx! {
            ItemGrid {
                items: items.to_vec(),
                group_by_day,
                selected,
                on_toggle_selected,
                on_delete,
                on_tags_changed,
                on_favorite_changed,
                on_edited,
                on_tag_click,
            }
        }
    }
}

/// The page's header. "Surprise me" calls `on_surprise`, and is disabled
/// unless `can_surprise` (the user has items). `initials`: the avatar's
/// letters.
#[component]
fn TopBar(initials: String, can_surprise: bool, on_surprise: EventHandler<()>) -> Element {
    let session = use_context::<AuthSession>();
    let mut menu_open = use_signal(|| false);
    let mut settings_open = use_signal(|| false);

    rsx! {
        header { class: "top-bar",
            div { class: "top-bar-brand",
                img { class: "top-bar-logo", src: LOGO_PNG, alt: "" }
                span { class: "top-bar-name", {t!("app-name")} }
            }

            div { class: "top-bar-actions",
                button {
                    class: "top-bar-surprise",
                    r#type: "button",
                    disabled: !can_surprise,
                    title: if can_surprise { t!("surprise-me-title") } else { t!("surprise-me-disabled") },
                    onclick: move |_| on_surprise.call(()),
                    span { class: "top-bar-surprise-star", "✦" }
                    span { {t!("surprise-me")} }
                }

                div { class: "top-bar-menu",
                    button {
                        class: "avatar-button",
                        r#type: "button",
                        title: t!("account-menu"),
                        onclick: move |_| menu_open.set(!menu_open()),
                        span { class: "avatar-initials", "{initials}" }
                    }

                if menu_open() {
                    // Invisible full-screen layer under the dropdown: a
                    // click anywhere outside the menu lands here and
                    // closes it.
                    div {
                        class: "menu-backdrop",
                        onclick: move |_| menu_open.set(false),
                    }
                    div { class: "menu-dropdown",
                        button {
                            class: "menu-item",
                            r#type: "button",
                            onclick: move |_| {
                                menu_open.set(false);
                                settings_open.set(true);
                            },
                            IconUser {}
                            {t!("account-settings")}
                        }
                        div { class: "menu-divider" }
                        button {
                            class: "menu-item menu-item-danger",
                            r#type: "button",
                            onclick: move |_| session.logout(),
                            IconLogout {}
                            {t!("account-logout")}
                        }
                    }
                }
                }
            }
        }

        // Outside the header: its sticky positioning makes it a stacking
        // context, which would keep the window below card menus.
        if settings_open() {
            AccountSettings { on_close: move |_| settings_open.set(false) }
        }
    }
}

/// The avatar's letters: the first two letters or digits of the email,
/// capitalized (`"ntnzelenin@gmail.com"` → `"NT"`).
fn email_initials(email: &str) -> String {
    email
        .chars()
        .filter(|c| c.is_alphanumeric())
        .take(2)
        .flat_map(char::to_uppercase)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initials_are_the_emails_first_two_letters() {
        assert_eq!(email_initials("ntnzelenin@gmail.com"), "NT");
        assert_eq!(email_initials("a.b@example.com"), "AB");
        assert_eq!(email_initials("x@example.com"), "XE");
        assert_eq!(email_initials(""), "");
    }

    #[test]
    fn delete_confirmation_says_how_many_items_go() {
        use crate::i18n::Language;
        use crate::i18n::tests::in_language;

        let title = |language, count: usize| {
            in_language(language, || t!("delete-confirm-title", count: count))
        };
        assert_eq!(title(Language::English, 1), "Delete this item?");
        assert_eq!(
            title(Language::English, 3),
            "Delete \u{2068}3\u{2069} items?"
        );
        assert_eq!(title(Language::Ukrainian, 1), "Видалити цей запис?");
        assert_eq!(
            title(Language::Ukrainian, 3),
            "Видалити \u{2068}3\u{2069} \u{2068}записи\u{2069}?"
        );
        assert_eq!(
            title(Language::Ukrainian, 21),
            "Видалити \u{2068}21\u{2069} \u{2068}запис\u{2069}?"
        );
        assert_eq!(
            title(Language::Ukrainian, 5),
            "Видалити \u{2068}5\u{2069} \u{2068}записів\u{2069}?"
        );
    }
}
