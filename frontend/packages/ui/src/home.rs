use std::time::Duration;

use api::{ItemQuery, ListedItem, Tag};
use base64::prelude::{BASE64_STANDARD, Engine as _};
use dioxus::html::{FileData, HasFileData};
use dioxus::prelude::*;
use futures_timer::Delay;

use crate::AuthSession;
use crate::filters::{FavoritesToggle, TagFilter, TypeDropdown, TypeFilter};
use crate::icons::{
    IconArrowUp, IconClose, IconFile, IconLogout, IconMenu, IconPaperclip, IconSearch, IconStash,
    IconTag, IconUser,
};
use crate::items::{ItemGrid, TagPicker};
use crate::routes::Route;

const FILE_UPLOAD_INPUT_ID: &str = "home-file-upload-input";
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

/// A file the user has picked or dropped but not yet sent.
///
/// The content type is a guess from the extension: the browser normally
/// supplies one via `File.type`, but Dioxus's cross-platform `FileData` only
/// exposes the name. The backend ignores it anyway and determines the real
/// type itself; for non-images it's just `application/octet-stream`.
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

impl PendingFile {
    fn is_image(&self) -> bool {
        self.preview_url.is_some()
    }
}

/// Reads `file` into a `PendingFile` ready for preview, without uploading
/// it. Shared by both the file-picker input and drag-and-drop, which differ
/// only in how they obtain the `FileData`.
async fn stage_file(file: FileData) -> Option<PendingFile> {
    let file_name = file.name();
    let data = file.read_bytes().await.ok()?.to_vec();

    let (content_type, preview_url) = match image_content_type(&file_name) {
        Some(content_type) => (
            content_type,
            Some(format!(
                "data:{content_type};base64,{}",
                BASE64_STANDARD.encode(&data)
            )),
        ),
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

    {
        let session = session.clone();
        use_effect(move || {
            if !session.is_authenticated() {
                nav.push(Route::Auth {});
            }
        });
    }

    // Type and tag filters. Applied by the server, to the list and to
    // search alike; changing either refetches whichever is showing.
    let active_type = use_signal(|| TypeFilter::All);
    let selected_tags = use_signal(Vec::<Tag>::new);
    let favorites_only = use_signal(|| false);
    let current_filters = move || ItemQuery {
        item_type: active_type().api_value().map(str::to_string),
        tag_ids: selected_tags().iter().map(|tag| tag.id.clone()).collect(),
        favorites_only: favorites_only(),
    };

    // Re-runs whenever the filters change (they're read below, outside the
    // async block, which is what subscribes to them). `submit`, deletes and
    // tag edits call `.restart()` so changes show up without a reload. No
    // pagination yet.
    let mut saved_items = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            let filters = current_filters();
            async move { session.list_items(None, 30, filters).await }
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

    let mut note = use_signal(String::new);
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

    // Deletes from a card's menu, then refetches whichever view is showing
    // (list and search), so the card disappears from both.
    let delete_item = use_callback({
        let session = session.clone();
        move |item_id: String| {
            let session = session.clone();
            spawn(async move {
                match session.delete_item(item_id).await {
                    Ok(()) => {
                        saved_items.restart();
                        search_results.restart();
                    }
                    Err(err) => status.set(Some(format!("Could not delete the item: {err}"))),
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
    });

    // A card's favorite state changed. The card already shows it, so only
    // refetch when showing favorites only, where it may need to drop out.
    let favorite_changed = use_callback(move |()| {
        if favorites_only() {
            saved_items.restart();
            search_results.restart();
        }
    });

    let mut submit = {
        let session = session.clone();
        move || {
            if is_submitting() {
                return;
            }

            // With files staged, the typed text is their caption: each file
            // becomes one item carrying both (image/file above, text
            // below), not a separate note.
            let files = std::mem::take(&mut *pending_files.write());
            if !files.is_empty() {
                let session = session.clone();
                let caption = Some(note().trim().to_string()).filter(|text| !text.is_empty());
                let tags = pending_tags();
                spawn(async move {
                    is_submitting.set(true);
                    status.set(None);

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
                                    tags.clone(),
                                )
                                .await
                        } else {
                            session
                                .create_file_item(
                                    &file.file_name,
                                    file.content_type,
                                    file.data,
                                    caption.clone(),
                                    tags.clone(),
                                )
                                .await
                        };
                        if let Err(err) = result {
                            last_error = Some(err.to_string());
                        }
                    }
                    // Keep the text and tags if anything failed, so they
                    // aren't lost.
                    if last_error.is_none() {
                        note.set(String::new());
                        pending_tags.set(Vec::new());
                    }
                    status.set(last_error);
                    saved_items.restart();
                    search_results.restart();

                    is_submitting.set(false);
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

                match session
                    .create_text_item(note().trim(), pending_tags())
                    .await
                {
                    Ok(_) => {
                        note.set(String::new());
                        pending_tags.set(Vec::new());
                        saved_items.restart();
                        search_results.restart();
                    }
                    Err(err) => status.set(Some(err.to_string())),
                }

                is_submitting.set(false);
            });
        }
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
                None => status.set(Some("Could not read the selected file".to_string())),
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
                    None => status.set(Some("Could not read the selected file".to_string())),
                }
            }
        });
    };

    rsx! {
        document::Link { rel: "stylesheet", href: HOME_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }

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

            TopBar {}

            div { class: "home-hero",
                IconStash {}
                h1 { class: "home-title", "stash" }
                p { class: "home-tagline", "Save anything. Find anytime." }

                if drag_depth() > 0 {
                    div { class: "home-drop-hint", "Drop files to upload" }
                }

                form {
                    class: "home-input-wrap",
                    onsubmit: move |evt| {
                        evt.prevent_default();
                        submit();
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
                                            title: "Remove file",
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
                        // Tags for what's sent next: chips (× removes)
                        // and, while picking, the tag picker.
                        if !pending_tags().is_empty() || picking_tag() {
                            div { class: "home-tags-row",
                                for (index , name) in pending_tags().into_iter().enumerate() {
                                    span { class: "tag-chip", key: "{name}", title: "{name}",
                                        span { class: "tag-chip-name", "{name}" }
                                        button {
                                            class: "tag-chip-remove",
                                            r#type: "button",
                                            title: "Remove tag",
                                            aria_label: "Remove tag {name}",
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
                            input {
                                class: "home-input",
                                placeholder: "Paste a link, write a note, or anything...",
                                value: "{note}",
                                oninput: move |evt| note.set(evt.value()),
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
                            button {
                                class: "home-input-attach",
                                r#type: "button",
                                title: "Add tags",
                                disabled: is_submitting(),
                                onclick: move |_| picking_tag.set(true),
                                IconTag {}
                            }
                            label {
                                class: "home-input-attach",
                                r#for: FILE_UPLOAD_INPUT_ID,
                                title: "Attach images or files",
                                IconPaperclip {}
                            }
                            button {
                                class: "home-input-submit",
                                r#type: "submit",
                                disabled: is_submitting(),
                                IconArrowUp {}
                            }
                        }
                    }
                }

                if let Some(message) = status() {
                    p { class: "home-status", "{message}" }
                }
            }

            // Divider marks the hand-off from "save new content" (the hero
            // above) to "search existing content" (the workspace below) —
            // a sibling of both, not nested in either, so its width is its
            // own rather than inherited from the narrow hero or the wider
            // workspace container.
            hr { class: "home-divider" }

            // Full-width workspace band (a sibling of the narrow hero, not
            // nested in it) so it can span the whole page and grow to fill
            // the rest of the viewport, with only the content inside it
            // kept to a centered — but wider-than-the-hero — reading width.
            div { class: "stash-main",
                div { class: "stash-section",
                    // [ Type ▾ ]  [ Search... ]  [ Tags ▾ ] — typing in the
                    // search swaps the list below for semantic search
                    // results; the type and tag filters apply to either.
                    div { class: "stash-controls",
                        div { class: "stash-controls-group",
                            TypeDropdown { value: active_type }
                            FavoritesToggle { value: favorites_only }
                        }
                        div { class: "stash-search-wrap",
                            IconSearch {}
                            input {
                                class: "stash-search-input",
                                r#type: "search",
                                placeholder: "Search your stash...",
                                value: "{search_query}",
                                oninput: move |evt| search_query.set(evt.value()),
                            }
                        }
                        TagFilter { selected: selected_tags }
                    }

                    if search_query().trim().is_empty() {
                        {match &*saved_items.read() {
                            None => rsx! {
                                div { class: "stash-empty",
                                    p { "Loading your stash..." }
                                }
                            },
                            Some(Err(err)) => rsx! {
                                div { class: "stash-empty",
                                    p { "Could not load your stash: {err}" }
                                }
                            },
                            Some(Ok(response)) if response.items.is_empty() && current_filters() == ItemQuery::default() => rsx! {
                                div { class: "stash-empty",
                                    IconStash {}
                                    p { "Nothing saved yet — items you capture will show up here." }
                                }
                            },
                            Some(Ok(response)) => item_results(
                                &response.items,
                                "Nothing matches these filters.".to_string(),
                                delete_item,
                                refresh_items,
                                favorite_changed,
                                refresh_items,
                            ),
                        }}
                    } else {
                        {match &*search_results.read() {
                            Some(Some((query, Ok(response)))) => item_results(
                                &response.items,
                                format!("Nothing matches “{query}”."),
                                delete_item,
                                refresh_items,
                                favorite_changed,
                                refresh_items,
                            ),
                            Some(Some((_, Err(err)))) => rsx! {
                                div { class: "stash-empty",
                                    p { "Search failed: {err}" }
                                }
                            },
                            // Debouncing, or the request is in flight.
                            _ => rsx! {
                                div { class: "stash-empty",
                                    p { "Searching..." }
                                }
                            },
                        }}
                    }
                }
            }
        }
    }
}

/// Renders a list page or search results (already filtered by the
/// server), or `empty_message` if there are none.
fn item_results(
    items: &[ListedItem],
    empty_message: String,
    on_delete: Callback<String>,
    on_tags_changed: Callback<()>,
    on_favorite_changed: Callback<()>,
    on_edited: Callback<()>,
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
                on_delete,
                on_tags_changed,
                on_favorite_changed,
                on_edited,
            }
        }
    }
}

#[component]
fn TopBar() -> Element {
    let session = use_context::<AuthSession>();
    let mut menu_open = use_signal(|| false);

    rsx! {
        header { class: "top-bar",
            div { class: "top-bar-brand",
                IconStash {}
                span { class: "top-bar-name", "stash" }
            }

            div { class: "top-bar-menu",
                button {
                    class: "menu-button",
                    r#type: "button",
                    onclick: move |_| menu_open.set(!menu_open()),
                    div { class: "menu-button-inner", IconMenu {} }
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
                        button { class: "menu-item", r#type: "button", IconUser {} "Account settings" }
                        div { class: "menu-divider" }
                        button {
                            class: "menu-item menu-item-danger",
                            r#type: "button",
                            onclick: move |_| session.logout(),
                            IconLogout {}
                            "Logout"
                        }
                    }
                }
            }
        }
    }
}
