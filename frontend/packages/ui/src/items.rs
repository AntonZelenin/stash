use api::{ItemUpdate, ListedItem, Tag, TextItemType};
use chrono::{DateTime, Local, TimeZone};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::filters::{TAG_LIST_LIMIT, TAG_SEARCH_DEBOUNCE};
use crate::i18n::{Language, api_error_message, current_language};
use crate::icons::{
    IconClose, IconFile, IconHeart, IconHeartFilled, IconLink, IconMoreHorizontal, IconPencil,
    IconTrash,
};
use crate::text_kind::{Segment, TextKind, first_url, link_segments, text_kind};

const ITEMS_CSS: Asset = asset!("/assets/styling/items.css");
/// Tag chips, shared with the other tag UI (see tags.css).
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

/// Narrowest a column is allowed to get before the grid drops to one fewer
/// column. Measured against the grid's own width, not the viewport, so the
/// grid adapts to wherever it's placed.
const MIN_COLUMN_WIDTH_PX: f64 = 240.0;
/// The usual desktop layout: as many columns as fit, up to this many.
const PREFERRED_MAX_COLUMNS: usize = 3;
/// Widest a column may get at `PREFERRED_MAX_COLUMNS` before the grid adds
/// one more column rather than stretching the cards further.
const MAX_COLUMN_WIDTH_PX: f64 = 440.0;
/// Hard cap. Together with `.stash-section`'s max-width, this keeps cards
/// from growing past MAX_COLUMN_WIDTH_PX on even the widest screens.
const MAX_COLUMNS: usize = 4;
/// Must match `.item-grid`'s `gap` in items.css.
const COLUMN_GAP_PX: f64 = 20.0;

fn column_count(grid_width: f64) -> usize {
    let column_width = |n: usize| (grid_width - (n - 1) as f64 * COLUMN_GAP_PX) / n as f64;
    // n columns fit when n * min + (n - 1) * gap <= width.
    let fitting = ((grid_width + COLUMN_GAP_PX) / (MIN_COLUMN_WIDTH_PX + COLUMN_GAP_PX)).floor();
    let mut count = (fitting as usize).clamp(1, PREFERRED_MAX_COLUMNS);
    // Fewer columns than preferred means they're already narrow; only a
    // full row of preferred columns can get too wide.
    while count >= PREFERRED_MAX_COLUMNS
        && count < MAX_COLUMNS
        && column_width(count) > MAX_COLUMN_WIDTH_PX
    {
        count += 1;
    }
    count
}

/// Masonry layout: equal-width columns, each an independent vertical stack,
/// so a card starts right below the previous card in its column instead of
/// waiting for the tallest card in its row.
///
/// Items are dealt into columns round-robin (item i goes to column
/// i % columns), not via CSS `columns`: that fills column 1 top-to-bottom
/// first, which for a newest-first feed would put the newest items all down
/// the left edge and reshuffle every column whenever an item is added.
/// Round-robin keeps the reading order row-by-row, like the old grid.
///
/// The column count comes from the grid's measured width (`onresize`),
/// since the number of column elements is decided here, not in CSS.
///
/// `on_delete` receives the id of an item the user chose to delete from
/// its card menu, `on_tags_changed` fires after a tag was added to or
/// removed from a card, `on_favorite_changed` after a card's favorite
/// state was saved, and `on_edited` after an item's content was edited; the
/// caller acts on them and refreshes `items` as needed. `on_tag_click`
/// receives a tag the user clicked on a card, to filter by it.
#[component]
pub fn ItemGrid(
    items: Vec<ListedItem>,
    on_delete: EventHandler<String>,
    on_tags_changed: EventHandler<()>,
    on_favorite_changed: EventHandler<()>,
    on_edited: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
) -> Element {
    let mut columns = use_signal(|| PREFERRED_MAX_COLUMNS);

    let current_columns = columns();
    let mut stacks: Vec<Vec<ListedItem>> = vec![Vec::new(); current_columns];
    for (index, item) in items.into_iter().enumerate() {
        stacks[index % current_columns].push(item);
    }

    rsx! {
        document::Link { rel: "stylesheet", href: ITEMS_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }

        div {
            class: "item-grid",
            onresize: move |evt| {
                if let Ok(size) = evt.get_content_box_size() {
                    let count = column_count(size.width);
                    if count != columns() {
                        columns.set(count);
                    }
                }
            },
            for (index , stack) in stacks.into_iter().enumerate() {
                div { class: "item-grid-column", key: "{index}",
                    for item in stack {
                        ItemCard {
                            key: "{item.id}",
                            item,
                            on_delete,
                            on_tags_changed,
                            on_favorite_changed,
                            on_edited,
                            on_tag_click,
                        }
                    }
                }
            }
        }
    }
}

/// When an item was saved, formatted for its card: a short date shown on the
/// card, and the full date and time for its tooltip.
#[derive(Clone, PartialEq)]
struct UploadDate {
    label: String,
    full: String,
}

impl UploadDate {
    /// `created_at` is the API's RFC 3339 timestamp; shown in the user's
    /// local time zone, so an item saved just before midnight isn't dated
    /// the next day, with month names in the UI language. None if the
    /// timestamp can't be parsed.
    fn from_api(created_at: &str) -> Option<Self> {
        Self::in_zone(created_at, &Local, current_language())
    }

    fn in_zone<Tz: TimeZone>(created_at: &str, zone: &Tz, language: Language) -> Option<Self>
    where
        Tz::Offset: std::fmt::Display,
    {
        let local = DateTime::parse_from_rfc3339(created_at)
            .ok()?
            .with_timezone(zone);
        let locale = language.chrono_locale();
        Some(Self {
            label: local.format_localized("%-d %b %Y", locale).to_string(),
            full: local
                .format_localized("%-d %B %Y, %H:%M", locale)
                .to_string(),
        })
    }
}

/// The upload date, in the bottom-right corner of a card's footer.
#[component]
fn CardDate(date: Option<UploadDate>) -> Element {
    match date {
        Some(date) => rsx! {
            span { class: "item-card-date", title: "{date.full}", "{date.label}" }
        },
        None => rsx! {},
    }
}

/// How an item is open in its `ItemView`.
#[derive(Clone, Copy, PartialEq)]
enum ViewMode {
    Viewing,
    Editing,
}

/// One saved item: its type-specific content, a footer with its tags,
/// favorite button and upload date, and its actions menu.
///
/// The card is a plain box; only the content part is clickable (a link or
/// file opens, an image opens the viewer), so the tag controls in the
/// footer never trigger it. The menu sits in a wrapper *beside* the card,
/// and nothing in the card clips its content, so dropdowns can extend past
/// short cards.
///
/// Clicking an image opens it in an `ItemView` with the same controls as
/// the card, as does clicking a note's (clamped) preview, to read the whole
/// text; "Edit" in the menu opens any item there, in edit mode. The
/// card owns the view, so both show one favorite state and share the same
/// handlers.
///
/// `on_tags_changed` fires after a tag was added to or removed from it,
/// `on_favorite_changed` after its favorite state was saved, and
/// `on_edited` after an edit was saved, and `on_tag_click` with a tag
/// clicked on the card or in its view (which then closes).
#[component]
fn ItemCard(
    item: ListedItem,
    on_delete: EventHandler<String>,
    on_tags_changed: EventHandler<()>,
    on_favorite_changed: EventHandler<()>,
    on_edited: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
) -> Element {
    // A saved edit's result, paired with the item it replaced: shown while
    // `item` is still that one, i.e. until the refetch `on_edited` asks for
    // arrives, so the old content doesn't flash back in the meantime.
    let mut edited = use_signal(|| None::<(ListedItem, ListedItem)>);
    let props_item = item;
    let item = match edited() {
        Some((before, after)) if before == props_item => after,
        _ => props_item.clone(),
    };
    let (is_favorite, toggle_favorite) =
        use_favorite(item.id.clone(), item.is_favorite, on_favorite_changed);
    // Whether the item is open in its `ItemView`, and how.
    let mut view = use_signal(|| None::<ViewMode>);

    // Grid cards show the small thumbnail; the full original is only the
    // fallback while the thumbnail is still being generated. Viewing an
    // image opens the original (see `OpenedItem`).
    let image_url = item.thumbnail_url.as_ref().or(item.download_url.as_ref());
    let date = UploadDate::from_api(&item.created_at);
    let (kind, body) = match (item.r#type.as_str(), image_url, &item.text) {
        ("image", Some(url), caption) => (
            "item-card-image",
            rsx! {
                ImageBody {
                    url: url.clone(),
                    caption: caption.clone(),
                    on_open: move |_| view.set(Some(ViewMode::Viewing)),
                }
            },
        ),
        ("link", _, Some(text)) => (
            "item-card-link",
            rsx! {
                LinkBody { text: text.clone() }
            },
        ),
        // Before the catch-all below: a file with a caption has `text`
        // too.
        ("file", _, caption) => match (&item.file, &item.download_url) {
            (Some(file), Some(url)) => (
                "item-card-file",
                rsx! {
                    FileBody {
                        url: url.clone(),
                        filename: file.filename.clone(),
                        size_bytes: file.size_bytes,
                        caption: caption.clone(),
                    }
                },
            ),
            _ => return rsx! {},
        },
        // A preview: clicking it opens the whole note in its `ItemView`.
        (_, _, Some(text)) => (
            "item-card-note",
            rsx! {
                div {
                    class: "item-card-note-main",
                    role: "button",
                    tabindex: "0",
                    onclick: move |_| view.set(Some(ViewMode::Viewing)),
                    onkeydown: move |evt| {
                        if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                            evt.prevent_default();
                            view.set(Some(ViewMode::Viewing));
                        }
                    },
                    p { class: "item-card-note-text", LinkedText { text: text.clone() } }
                }
            },
        ),
        // e.g. an image whose download URL couldn't be produced.
        _ => return rsx! {},
    };

    let item_id = item.id.clone();
    rsx! {
        div { class: "item-card-shell",
            div { class: "item-card {kind}",
                {body}
                div { class: "item-card-footer",
                    ItemTags {
                        item_id: item.id.clone(),
                        tags: item.tags.clone(),
                        on_changed: on_tags_changed,
                        on_tag_click,
                    }
                    // Bottom-right: ♡ then the upload date.
                    div { class: "item-card-meta",
                        FavoriteButton { is_favorite, on_toggle: toggle_favorite }
                        CardDate { date }
                    }
                }
            }
            // Top-right, over the card: ⋯.
            div { class: "item-card-actions",
                ItemMenu {
                    can_edit: true,
                    on_edit: move |_| view.set(Some(ViewMode::Editing)),
                    on_delete: {
                        let item_id = item_id.clone();
                        move |_| on_delete.call(item_id.clone())
                    },
                }
            }
            if let Some(mode) = view() {
                OpenedItem {
                    item: item.clone(),
                    mode,
                    is_favorite,
                    on_toggle_favorite: toggle_favorite,
                    on_mode: move |mode| view.set(mode),
                    on_delete: move |_| on_delete.call(item_id.clone()),
                    on_tags_changed,
                    on_tag_click,
                    on_saved: move |updated: ListedItem| {
                        edited.set(Some((props_item.clone(), updated)));
                        on_edited.call(());
                    },
                }
            }
        }
    }
}

/// An item opened on its own, outside the grid ("Surprise me"), in view
/// mode: the same view and controls as opening its card. It keeps itself
/// up to date: after an edit or a tag change it shows the item as saved.
///
/// `on_close` fires when it's closed (including by deleting the item or
/// clicking a tag); `on_delete` receives the item's id to delete,
/// `on_changed` fires after any change to the item was saved (so the page
/// can refetch), and `on_tag_click` receives a clicked tag.
#[component]
pub fn ItemViewer(
    item: ListedItem,
    on_close: EventHandler<()>,
    on_delete: EventHandler<String>,
    on_changed: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut mode = use_signal(|| ViewMode::Viewing);
    // The item as last saved here; the prop until something changes.
    let mut current = use_signal(|| item.clone());
    let item = current();
    let (is_favorite, toggle_favorite) =
        use_favorite(item.id.clone(), item.is_favorite, on_changed);

    // A tag was added or removed: fetch the item again for its tags.
    let tags_changed = {
        let item_id = item.id.clone();
        move |_: ()| {
            let session = session.clone();
            let item_id = item_id.clone();
            spawn(async move {
                if let Ok(Some(updated)) = session.get_item(item_id).await {
                    current.set(updated);
                }
            });
            on_changed.call(());
        }
    };

    rsx! {
        OpenedItem {
            item: item.clone(),
            mode: mode(),
            is_favorite,
            on_toggle_favorite: toggle_favorite,
            on_mode: move |next: Option<ViewMode>| match next {
                Some(next) => mode.set(next),
                None => on_close.call(()),
            },
            on_delete: {
                let item_id = item.id.clone();
                move |_| on_delete.call(item_id.clone())
            },
            on_tags_changed: tags_changed,
            on_tag_click,
            on_saved: move |updated: ListedItem| {
                current.set(updated);
                on_changed.call(());
            },
        }
    }
}

/// The item's favorite state for its controls, and a toggle that saves it.
/// The user's latest choice is shown immediately (optimistically) while it
/// saves, and reverted if saving fails; `on_changed` fires once it's saved.
fn use_favorite(
    item_id: String,
    saved: bool,
    on_changed: EventHandler<()>,
) -> (bool, Callback<()>) {
    let session = use_context::<AuthSession>();
    // None until the user toggles, i.e. show the server's value.
    let mut favorite_override = use_signal(|| None::<bool>);
    let mut favorite_saving = use_signal(|| false);
    let is_favorite = favorite_override().unwrap_or(saved);

    let toggle = use_callback(move |()| {
        if favorite_saving() {
            return;
        }
        let session = session.clone();
        let item_id = item_id.clone();
        let previous = is_favorite;
        let wanted = !previous;
        favorite_override.set(Some(wanted));
        spawn(async move {
            favorite_saving.set(true);
            match session.set_favorite(item_id, wanted).await {
                Ok(()) => on_changed.call(()),
                // Didn't stick: show the real state again.
                Err(_) => favorite_override.set(Some(previous)),
            }
            favorite_saving.set(false);
        });
    });
    (is_favorite, toggle)
}

/// An item open in its `ItemView`, in `mode`: its date, favorite button
/// and menu above its full content and tags (viewing), or its edit form
/// (editing).
///
/// `on_mode` asks to switch mode, or to close (None). `on_delete` asks to
/// delete the item (the view closes first), `on_saved` receives the item
/// as saved by an edit, and `on_tag_click` a clicked tag (the view closes
/// first, so the filtered results behind it show).
#[component]
fn OpenedItem(
    item: ListedItem,
    mode: ViewMode,
    is_favorite: bool,
    on_toggle_favorite: EventHandler<()>,
    on_mode: EventHandler<Option<ViewMode>>,
    on_delete: EventHandler<()>,
    on_tags_changed: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
    on_saved: EventHandler<ListedItem>,
) -> Element {
    // The view shows the full original image, not the thumbnail.
    let full_size_url = match item.r#type.as_str() {
        "image" => item.download_url.clone().or(item.thumbnail_url.clone()),
        _ => None,
    };
    let is_note = !matches!(item.r#type.as_str(), "image" | "link" | "file");

    rsx! {
        ItemView {
            image_url: full_size_url,
            editing: mode == ViewMode::Editing,
            reading: is_note && mode == ViewMode::Viewing,
            on_close: move |_| on_mode.call(None),
            on_cancel_edit: move |_| on_mode.call(Some(ViewMode::Viewing)),
            // The card's controls again, above the content.
            div { class: "lightbox-panel-header",
                CardDate { date: UploadDate::from_api(&item.created_at) }
                div { class: "lightbox-actions",
                    FavoriteButton { is_favorite, on_toggle: on_toggle_favorite }
                    ItemMenu {
                        can_edit: mode == ViewMode::Viewing,
                        on_edit: move |_| on_mode.call(Some(ViewMode::Editing)),
                        on_delete: move |_| {
                            on_mode.call(None);
                            on_delete.call(());
                        },
                    }
                }
            }
            match mode {
                ViewMode::Viewing => rsx! {
                    ItemDetails { item: item.clone() }
                    ItemTags {
                        item_id: item.id.clone(),
                        tags: item.tags.clone(),
                        on_changed: on_tags_changed,
                        on_tag_click: move |tag| {
                            on_mode.call(None);
                            on_tag_click.call(tag);
                        },
                    }
                },
                ViewMode::Editing => rsx! {
                    ItemEditor {
                        item: item.clone(),
                        on_tags_changed,
                        on_cancel: move |_| on_mode.call(Some(ViewMode::Viewing)),
                        on_saved: move |updated: Option<ListedItem>| {
                            if let Some(updated) = updated {
                                on_saved.call(updated);
                            }
                            on_mode.call(Some(ViewMode::Viewing));
                        },
                    }
                },
            }
        }
    }
}

/// The item's content in its `ItemView`, in full: an image's caption (the
/// image itself is beside it), a note's whole text, a link, or a file with
/// its caption.
#[component]
fn ItemDetails(item: ListedItem) -> Element {
    match (
        item.r#type.as_str(),
        &item.file,
        &item.download_url,
        item.text,
    ) {
        ("link", _, _, Some(text)) => rsx! {
            LinkBody { text }
        },
        ("file", Some(file), Some(url), caption) => rsx! {
            FileBody {
                url: url.clone(),
                filename: file.filename.clone(),
                size_bytes: file.size_bytes,
                caption,
            }
        },
        (_, _, _, Some(text)) => rsx! {
            p { class: "lightbox-text", LinkedText { text } }
        },
        _ => rsx! {},
    }
}

/// Form for the item's editable content, in its `ItemView`: a note's or
/// link's whole text, an image's caption, or a file's name and caption.
/// A caption can be added, changed or cleared. A note's/link's type follows
/// from its edited text (a bare URL is a link, text without URLs a note);
/// for text mixing both, a Text/Link choice is shown, starting at the
/// item's current type. The text or caption field has focus when the form
/// opens. Below it, the item's tags, each with a × to remove it, and an
/// "Add tag" control; tag changes are only applied on Save, so Cancel
/// undoes them too.
///
/// Only changed fields are sent. `on_saved` gets the updated item, or None
/// if nothing had changed; `on_cancel` drops the changes. If a tag change
/// fails, the form stays open and `on_tags_changed` lets the page refetch,
/// as the tag changes before it were applied.
#[component]
fn ItemEditor(
    item: ListedItem,
    on_tags_changed: EventHandler<()>,
    on_cancel: EventHandler<()>,
    on_saved: EventHandler<Option<ListedItem>>,
) -> Element {
    let session = use_context::<AuthSession>();
    let original_text = item.text.clone().unwrap_or_default();
    let original_filename = item.file.as_ref().map(|file| file.filename.clone());
    let mut text = use_signal(|| original_text.clone());
    let mut filename = use_signal(|| original_filename.clone().unwrap_or_default());
    let original_type = TextItemType::from_api(&item.r#type);
    let mut chosen_type = use_signal(|| original_type.unwrap_or(TextItemType::Text));
    let mut saving = use_signal(|| false);
    let mut error = use_signal(|| None::<String>);
    // Tag changes made in this form, applied on Save: ids of the item's
    // tags ×'d, and names of tags added.
    let mut removed_tag_ids = use_signal(Vec::<String>::new);
    let mut added_tag_names = use_signal(Vec::<String>::new);
    let mut adding_tag = use_signal(|| false);
    let kept_tags: Vec<Tag> = item
        .tags
        .iter()
        .filter(|tag| !removed_tag_ids.read().contains(&tag.id))
        .cloned()
        .collect();
    let shown_tag_names: Vec<String> = kept_tags
        .iter()
        .map(|tag| tag.name.clone())
        .chain(added_tag_names())
        .collect();
    let add_tag = {
        let tags = item.tags.clone();
        move |name: String| {
            adding_tag.set(false);
            // Picking a tag ×'d here just brings it back.
            let lowercase = name.to_lowercase();
            match tags.iter().find(|tag| tag.name.to_lowercase() == lowercase) {
                Some(tag) => removed_tag_ids.write().retain(|id| *id != tag.id),
                None => added_tag_names.write().push(name),
            }
        }
    };

    let kind = item.r#type.as_str();
    let edits_text = matches!(kind, "text" | "link");
    let edits_filename = kind == "file";
    let edits_caption = matches!(kind, "image" | "file");
    let chooses_type = edits_text && text_kind(&text()) == TextKind::Mixed;

    let update = ItemUpdate {
        text: (edits_text || edits_caption)
            .then(|| text().trim().to_string())
            .filter(|text| text != original_text.trim()),
        filename: edits_filename
            .then(|| filename().trim().to_string())
            .filter(|name| Some(name) != original_filename.as_ref()),
        // Unsent, the server keeps the current type for mixed text.
        item_type: chooses_type
            .then_some(chosen_type())
            .filter(|chosen| Some(*chosen) != original_type),
    };
    let invalid = (edits_text && text().trim().is_empty())
        || (edits_filename && filename().trim().is_empty());

    let save = use_callback({
        let item = item.clone();
        let kept_tags = kept_tags.clone();
        move |()| {
            if saving() || invalid {
                return;
            }
            let removed = removed_tag_ids();
            let added = added_tag_names();
            if update == ItemUpdate::default() && removed.is_empty() && added.is_empty() {
                on_saved.call(None);
                return;
            }
            let session = session.clone();
            let item = item.clone();
            let kept_tags = kept_tags.clone();
            let update = update.clone();
            spawn(async move {
                saving.set(true);
                // Tags first, so an updated item from the server below
                // already has them right. Both calls are no-ops when
                // repeated, so saving again after a failure is safe.
                let mut tags = kept_tags;
                let mut failure = None;
                for tag_id in removed {
                    if let Err(err) = session.remove_tag(item.id.clone(), tag_id).await {
                        failure = Some(err);
                        break;
                    }
                }
                if failure.is_none() {
                    for name in added {
                        match session.assign_tag(item.id.clone(), name).await {
                            Ok(tag) => tags.push(tag),
                            Err(err) => {
                                failure = Some(err);
                                break;
                            }
                        }
                    }
                }
                if let Some(err) = failure {
                    on_tags_changed.call(());
                    error.set(Some(api_error_message(&err)));
                    saving.set(false);
                    return;
                }
                if update == ItemUpdate::default() {
                    // Only tags changed: the item as it now is, its tags
                    // sorted by name as the server lists them.
                    tags.sort_by_key(|tag| tag.name.to_lowercase());
                    on_saved.call(Some(ListedItem { tags, ..item }));
                    return;
                }
                match session.update_item(item.id, update).await {
                    // This form closes; nothing more to reset here.
                    Ok(updated) => on_saved.call(Some(updated)),
                    Err(err) => {
                        error.set(Some(api_error_message(&err)));
                        saving.set(false);
                    }
                }
            });
        }
    });
    // Ctrl/⌘+Enter saves from a text area (plain Enter is a new line).
    let save_on_shortcut = move |evt: KeyboardEvent| {
        let modifiers = evt.modifiers();
        if evt.key() == Key::Enter && (modifiers.ctrl() || modifiers.meta()) {
            evt.prevent_default();
            save.call(());
        }
    };

    rsx! {
        div { class: "item-editor",
            if let Some(file) = item.file.as_ref().filter(|_| edits_filename) {
                div { class: "item-editor-file",
                    span { class: "item-card-file-icon", IconFile {} }
                    span { class: "item-card-file-details",
                        "{file_details(&file.filename, file.size_bytes)}"
                    }
                }
                label { class: "item-editor-field",
                    span { class: "item-editor-label", {t!("item-field-filename")} }
                    input {
                        class: "item-editor-input",
                        r#type: "text",
                        maxlength: "255",
                        value: "{filename}",
                        disabled: saving(),
                        oninput: move |evt| filename.set(evt.value()),
                        onkeydown: move |evt| {
                            if evt.key() == Key::Enter {
                                evt.prevent_default();
                                save.call(());
                            }
                        },
                    }
                }
            }
            if edits_text || edits_caption {
                label { class: "item-editor-field",
                    span { class: "item-editor-label",
                        if edits_text {
                            {t!("item-field-text")}
                        } else {
                            {t!("item-field-caption")}
                        }
                    }
                    textarea {
                        class: if edits_text { "item-editor-input item-editor-textarea item-editor-textarea-tall" } else { "item-editor-input item-editor-textarea" },
                        placeholder: if edits_text { String::new() } else { t!("item-caption-placeholder") },
                        value: "{text}",
                        disabled: saving(),
                        oninput: move |evt| text.set(evt.value()),
                        onmounted: move |evt| async move {
                            let _ = evt.set_focus(true).await;
                        },
                        onkeydown: save_on_shortcut,
                    }
                }
            }
            div { class: "item-editor-field",
                span { class: "item-editor-label", {t!("item-field-tags")} }
                div { class: "item-editor-tags",
                    for tag in kept_tags {
                        RemovableTagChip {
                            key: "{tag.id}",
                            name: tag.name.clone(),
                            disabled: saving(),
                            on_remove: move |_| removed_tag_ids.write().push(tag.id.clone()),
                        }
                    }
                    for (index , name) in added_tag_names().into_iter().enumerate() {
                        RemovableTagChip {
                            key: "added-{name}",
                            name,
                            disabled: saving(),
                            on_remove: move |_| {
                                added_tag_names.write().remove(index);
                            },
                        }
                    }
                    if adding_tag() {
                        TagPicker {
                            exclude: shown_tag_names,
                            busy: saving(),
                            on_pick: add_tag,
                            on_close: move |_| adding_tag.set(false),
                        }
                    } else {
                        button {
                            class: "tag-add",
                            r#type: "button",
                            disabled: saving(),
                            onclick: move |_| adding_tag.set(true),
                            {t!("tags-add-button")}
                        }
                    }
                }
            }
            if chooses_type {
                label { class: "item-editor-field",
                    span { class: "item-editor-label", {t!("item-field-type")} }
                    TextTypeSelect {
                        class: "item-editor-input",
                        value: chosen_type(),
                        disabled: saving(),
                        on_change: move |value| chosen_type.set(value),
                    }
                }
            }
            if let Some(message) = error() {
                p { class: "item-editor-error", "{message}" }
            }
            div { class: "item-editor-buttons",
                button {
                    class: "item-editor-button item-editor-cancel",
                    r#type: "button",
                    disabled: saving(),
                    onclick: move |_| on_cancel.call(()),
                    {t!("common-cancel")}
                }
                button {
                    class: "item-editor-button item-editor-save",
                    r#type: "button",
                    disabled: saving() || invalid,
                    onclick: move |_| save.call(()),
                    if saving() {
                        {t!("common-saving")}
                    } else {
                        {t!("common-save")}
                    }
                }
            }
        }
    }
}

/// A tag chip with a × after its name, calling `on_remove`.
#[component]
fn RemovableTagChip(name: String, disabled: bool, on_remove: EventHandler<()>) -> Element {
    rsx! {
        span { class: "tag-chip", title: "{name}",
            span { class: "tag-chip-name", "{name}" }
            button {
                class: "tag-chip-remove",
                r#type: "button",
                title: t!("tags-remove"),
                aria_label: t!("tags-remove-named", name: name.as_str()),
                disabled,
                onclick: move |_| on_remove.call(()),
                IconClose {}
            }
        }
    }
}

/// ♡ button, a bare icon: outlined, or filled red while the item is a
/// favorite.
#[component]
fn FavoriteButton(is_favorite: bool, on_toggle: EventHandler<()>) -> Element {
    let label = if is_favorite {
        t!("item-unlike")
    } else {
        t!("item-like")
    };

    rsx! {
        button {
            class: if is_favorite { "item-favorite-button item-favorite-active" } else { "item-favorite-button" },
            r#type: "button",
            title: label.clone(),
            aria_label: label,
            aria_pressed: if is_favorite { "true" } else { "false" },
            onclick: move |_| on_toggle.call(()),
            if is_favorite {
                IconHeartFilled {}
            } else {
                IconHeart {}
            }
        }
    }
}

/// "⋯" button in a card's top-right corner, opening a small actions menu.
/// Shown on hover (always on touch screens, which can't hover). "Edit" is
/// left out when `can_edit` is false (the item is already being edited).
#[component]
fn ItemMenu(can_edit: bool, on_edit: EventHandler<()>, on_delete: EventHandler<()>) -> Element {
    let mut open = use_signal(|| false);

    rsx! {
        div { class: if open() { "item-menu item-menu-open" } else { "item-menu" },
            button {
                class: "item-menu-button",
                r#type: "button",
                title: t!("item-more-actions"),
                onclick: move |_| open.toggle(),
                IconMoreHorizontal {}
            }
            if open() {
                // Invisible full-screen layer under the dropdown: clicking
                // anywhere outside the menu lands here and closes it.
                div {
                    class: "item-menu-backdrop",
                    onclick: move |_| open.set(false),
                }
                div { class: "item-menu-dropdown",
                    if can_edit {
                        button {
                            class: "item-menu-entry",
                            r#type: "button",
                            onclick: move |_| {
                                open.set(false);
                                on_edit.call(());
                            },
                            IconPencil {}
                            {t!("item-edit")}
                        }
                    }
                    button {
                        class: "item-menu-entry item-menu-entry-danger",
                        r#type: "button",
                        onclick: move |_| {
                            open.set(false);
                            on_delete.call(());
                        },
                        IconTrash {}
                        {t!("item-delete")}
                    }
                }
            }
        }
    }
}

/// The item's tags as chips, plus an "Add tag" control. Tags are removed in
/// the `ItemEditor`. An added tag goes straight to the server; `on_changed`
/// then lets the page refetch, which also keeps tag filters honest.
/// Clicking a chip calls `on_tag_click` with its tag.
#[component]
fn ItemTags(
    item_id: String,
    tags: Vec<Tag>,
    on_changed: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut adding = use_signal(|| false);
    let mut busy = use_signal(|| false);
    let mut error = use_signal(|| None::<String>);

    let assign = {
        let session = session.clone();
        let item_id = item_id.clone();
        move |name: String| {
            if busy() {
                return;
            }
            let session = session.clone();
            let item_id = item_id.clone();
            spawn(async move {
                busy.set(true);
                match session.assign_tag(item_id, name).await {
                    Ok(_) => {
                        error.set(None);
                        adding.set(false);
                        on_changed.call(());
                    }
                    Err(err) => error.set(Some(api_error_message(&err))),
                }
                busy.set(false);
            });
        }
    };

    let assigned_names: Vec<String> = tags.iter().map(|tag| tag.name.clone()).collect();

    rsx! {
        div { class: "item-tags",
            for tag in tags {
                button {
                    class: "tag-chip tag-chip-link",
                    key: "{tag.id}",
                    r#type: "button",
                    title: t!("tags-show-tagged", name: tag.name.as_str()),
                    onclick: move |_| on_tag_click.call(tag.clone()),
                    span { class: "tag-chip-name", "{tag.name}" }
                }
            }
            if adding() {
                TagPicker {
                    item_id: item_id.clone(),
                    exclude: assigned_names,
                    busy: busy(),
                    on_pick: assign,
                    on_close: move |_| adding.set(false),
                }
            } else {
                button {
                    class: "tag-add",
                    r#type: "button",
                    onclick: move |_| {
                        error.set(None);
                        adding.set(true);
                    },
                    {t!("tags-add-button")}
                }
            }
            if let Some(message) = error() {
                span { class: "item-tags-error", "{message}" }
            }
        }
    }
}

/// Collapses whitespace like the server does, for comparing what's typed
/// with existing tag names.
fn clean_tag_name(raw: &str) -> String {
    raw.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// How many suggested tags to show: in the tag picker before anything is
/// typed, and on the capture box's "Suggested" line.
pub(crate) const TAG_SUGGESTION_LIMIT: u32 = 6;

/// The user's suggested tags (recently, then frequently used) minus
/// `exclude` (names, compared case-insensitively), at most
/// `TAG_SUGGESTION_LIMIT`. With `item_id`, the server also leaves out that
/// item's tags. Asks for extra so excluded ones don't shorten the list.
pub(crate) async fn suggested_tags(
    session: &AuthSession,
    item_id: Option<String>,
    exclude: &[String],
) -> Result<Vec<Tag>, api::ApiError> {
    let excluded: Vec<String> = exclude.iter().map(|name| name.to_lowercase()).collect();
    let limit = (TAG_SUGGESTION_LIMIT + excluded.len() as u32).min(20);
    let tags = session.suggest_tags(item_id, limit).await?;
    Ok(tags
        .into_iter()
        .filter(|tag| !excluded.contains(&tag.name.to_lowercase()))
        .take(TAG_SUGGESTION_LIMIT as usize)
        .collect())
}

/// Input for choosing a tag to add. Until something is typed, suggested
/// tags (recently, then frequently used; see `suggested_tags`) are listed
/// below it; after, existing tags containing the typed text. Either list
/// leaves out `exclude` (names already chosen, compared case-insensitively)
/// and, with `item_id`, that item's tags. Picking one, or "Create …" when
/// nothing matches exactly, calls `on_pick` with the name. Enter picks the
/// exact match or creates (and never submits a surrounding form); Escape
/// or a click outside closes it. Used on item cards and in the capture box.
#[component]
pub(crate) fn TagPicker(
    #[props(default)] item_id: Option<String>,
    exclude: Vec<String>,
    busy: bool,
    on_pick: EventHandler<String>,
    on_close: EventHandler<()>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut query = use_signal(String::new);

    let exclude_for_fetch = exclude.clone();
    let matches = use_resource(move || {
        let session = session.clone();
        let item_id = item_id.clone();
        let exclude = exclude_for_fetch.clone();
        let query = clean_tag_name(&query());
        async move {
            if query.is_empty() {
                return suggested_tags(&session, item_id, &exclude).await;
            }
            Delay::new(TAG_SEARCH_DEBOUNCE).await;
            session.list_tags(query, TAG_LIST_LIMIT).await
        }
    });

    let typed = clean_tag_name(&query());
    let excluded: Vec<String> = exclude.iter().map(|name| name.to_lowercase()).collect();
    let (found, exact_exists) = match &*matches.read() {
        Some(Ok(tags)) => (
            tags.iter()
                .filter(|tag| !excluded.contains(&tag.name.to_lowercase()))
                .cloned()
                .collect::<Vec<_>>(),
            tags.iter()
                .any(|tag| tag.name.to_lowercase() == typed.to_lowercase()),
        ),
        _ => (Vec::new(), false),
    };
    let suggesting = typed.is_empty() && !found.is_empty();
    let can_create = !typed.is_empty() && !exact_exists;
    let show_menu = !found.is_empty() || can_create;
    // Cloned up front: `rsx!` doesn't evaluate in source order, and the
    // list below consumes `found`.
    let enter_matches = found.clone();
    let enter_typed = typed.clone();

    rsx! {
        div { class: "tag-picker",
            div { class: "tag-picker-backdrop", onclick: move |_| on_close.call(()) }
            input {
                class: "tag-picker-input",
                r#type: "text",
                placeholder: t!("tags-name-placeholder"),
                maxlength: "50",
                disabled: busy,
                value: "{query}",
                oninput: move |evt| query.set(evt.value()),
                onmounted: move |evt| async move {
                    let _ = evt.set_focus(true).await;
                },
                onkeydown: {
                    let found = enter_matches;
                    let typed = enter_typed;
                    move |evt: KeyboardEvent| {
                        if evt.key() == Key::Escape {
                            // Close just the picker, not a viewer it's in.
                            evt.stop_propagation();
                            on_close.call(());
                        } else if evt.key() == Key::Enter {
                            // Inside a form, Enter would submit it.
                            evt.prevent_default();
                            if typed.is_empty() {
                                return;
                            }
                            // An existing tag (in its stored spelling) if
                            // the name matches one, otherwise a new one; the
                            // server matches case-insensitively either way.
                            let name = found
                                .iter()
                                .find(|tag| tag.name.to_lowercase() == typed.to_lowercase())
                                .map(|tag| tag.name.clone())
                                .unwrap_or_else(|| typed.clone());
                            on_pick.call(name);
                        }
                    }
                },
            }
            if show_menu {
                div { class: "tag-picker-menu",
                    if suggesting {
                        p { class: "tag-picker-heading", {t!("tags-suggested")} }
                    }
                    for tag in found {
                        button {
                            class: "tag-picker-option",
                            key: "{tag.id}",
                            r#type: "button",
                            disabled: busy,
                            onclick: move |_| on_pick.call(tag.name.clone()),
                            "{tag.name}"
                        }
                    }
                    if can_create {
                        button {
                            class: "tag-picker-option tag-picker-create",
                            r#type: "button",
                            disabled: busy,
                            onclick: {
                                let typed = typed.clone();
                                move |_| on_pick.call(typed.clone())
                            },
                            {t!("tags-create", name: typed.as_str())}
                        }
                    }
                }
            }
        }
    }
}

/// Edge-to-edge image at its natural aspect ratio. Extreme ratios are
/// cropped (not squashed) to the min/max heights in items.css, so a very
/// tall screenshot can't take over a column and a panorama doesn't shrink
/// to a sliver. The user's caption, if any, sits below the image.
///
/// Clicking the image or caption calls `on_open`.
#[component]
fn ImageBody(url: String, caption: Option<String>, on_open: EventHandler<()>) -> Element {
    rsx! {
        div {
            class: "item-card-image-main",
            onclick: move |_| on_open.call(()),
            img { src: "{url}", alt: t!("item-image-alt"), loading: "lazy" }
            if let Some(caption) = caption {
                p { class: "item-card-image-caption", "{caption}" }
            }
        }
    }
}

/// An item opened over the whole page, on a dimmed backdrop: a panel with
/// `children` (the item's content and controls) and, for an image, the
/// image itself (scaled down to fit the screen if needed, never up) with
/// the panel beside it on wide screens and below it on narrow ones.
///
/// The ✕ button, Escape and a click anywhere on the backdrop close it.
/// While `editing`, Escape calls `on_cancel_edit` instead (like the form's
/// Cancel button), and backdrop clicks are ignored so a stray click doesn't
/// throw the changes away.
///
/// `reading` (a note being read) widens the panel for its text and lets it
/// grow to the text's full length: the backdrop then scrolls, rather than
/// the text inside the panel.
#[component]
fn ItemView(
    image_url: Option<String>,
    editing: bool,
    #[props(default)] reading: bool,
    on_close: EventHandler<()>,
    on_cancel_edit: EventHandler<()>,
    children: Element,
) -> Element {
    let has_image = image_url.is_some();
    let mut dialog = use_signal(|| None::<std::rc::Rc<MountedData>>);

    // Keep keyboard focus on the dialog whenever it's not being edited, so
    // Escape reaches it: on opening, and after leaving edit mode, when the
    // focused form field (or Cancel/Save button) has just been removed.
    // While editing, the form's field has focus instead.
    use_effect(use_reactive!(|editing| {
        if !editing && let Some(dialog) = dialog() {
            spawn(async move {
                let _ = dialog.set_focus(true).await;
            });
        }
    }));

    rsx! {
        div {
            class: if reading { "lightbox lightbox-reading" } else { "lightbox" },
            role: "dialog",
            aria_modal: "true",
            aria_label: if has_image { t!("item-image-viewer") } else { t!("item-viewer") },
            // Focusable so it can receive Escape (see the effect above).
            tabindex: "-1",
            onmounted: move |evt| dialog.set(Some(evt.data())),
            onkeydown: move |evt| {
                if evt.key() == Key::Escape {
                    if editing {
                        on_cancel_edit.call(());
                    } else {
                        on_close.call(());
                    }
                }
            },
            // The backdrop is the lightbox itself, so any click that isn't
            // stopped by the content below lands here.
            onclick: move |_| {
                if !editing {
                    on_close.call(());
                }
            },
            div {
                class: if has_image { "lightbox-content" } else { "lightbox-content lightbox-content-no-media" },
                // Also catches clicks on the full-screen backdrops of the
                // menu and tag picker inside, which only close those.
                onclick: move |evt| evt.stop_propagation(),
                if let Some(url) = image_url {
                    div { class: "lightbox-media",
                        img {
                            class: "lightbox-image",
                            src: "{url}",
                            alt: t!("item-image-alt"),
                        }
                    }
                }
                div { class: "lightbox-panel", {children} }
            }
            button {
                class: "lightbox-close",
                r#type: "button",
                title: t!("common-close"),
                aria_label: t!("common-close"),
                onclick: move |evt| {
                    evt.stop_propagation();
                    on_close.call(());
                },
                IconClose {}
            }
        }
    }
}

/// An uploaded file: file icon, original filename and its type/size, plus
/// the caption if one was added. Links to the file's download URL, which the
/// backend signs so it opens in a new tab where the browser can show it
/// (PDF, text) and downloads under its original name otherwise.
#[component]
fn FileBody(url: String, filename: String, size_bytes: u64, caption: Option<String>) -> Element {
    let details = file_details(&filename, size_bytes);

    rsx! {
        a {
            class: "item-card-file-main",
            href: "{url}",
            target: "_blank",
            rel: "noopener noreferrer",
            title: "{filename}",
            span { class: "item-card-file-row",
                span { class: "item-card-file-icon", IconFile {} }
                span { class: "item-card-file-body",
                    span { class: "item-card-file-name", "{filename}" }
                    span { class: "item-card-file-details", "{details}" }
                }
            }
            if let Some(caption) = caption {
                span { class: "item-card-file-caption", "{caption}" }
            }
        }
    }
}

/// "PDF · 1.2 MB": the extension as a type label, plus a readable size in
/// the UI language.
fn file_details(filename: &str, size_bytes: u64) -> String {
    let size = format_size(size_bytes, current_language());
    match filename.rsplit_once('.') {
        Some((stem, extension)) if !stem.is_empty() && !extension.is_empty() => {
            format!("{} · {size}", extension.to_uppercase())
        }
        _ => size,
    }
}

fn format_size(bytes: u64, language: Language) -> String {
    const KB: f64 = 1024.0;
    const MB: f64 = KB * 1024.0;
    let bytes_f = bytes as f64;
    if bytes_f >= MB {
        t!("size-megabytes", size: language.format_decimal(bytes_f / MB, 1))
    } else if bytes_f >= KB {
        t!("size-kilobytes", size: language.format_decimal(bytes_f / KB, 0))
    } else {
        t!("size-bytes", size: bytes.to_string())
    }
}

/// Text/Link choice for a note mixing text and URLs; `class` styles the
/// `select` for where it's shown.
#[component]
pub(crate) fn TextTypeSelect(
    class: &'static str,
    value: TextItemType,
    disabled: bool,
    on_change: EventHandler<TextItemType>,
) -> Element {
    rsx! {
        select {
            class,
            title: t!("item-save-as"),
            aria_label: t!("item-save-as"),
            disabled,
            value: value.as_api(),
            onchange: move |evt| {
                if let Some(chosen) = TextItemType::from_api(&evt.value()) {
                    on_change.call(chosen);
                }
            },
            option { value: "text", selected: value == TextItemType::Text, {t!("item-type-text")} }
            option { value: "link", selected: value == TextItemType::Link, {t!("item-type-link")} }
        }
    }
}

/// A saved link: its (first) URL, and the whole text below it when there's
/// more than the URL. The API only stores the text (no page title or
/// preview image yet), so the domain stands in as the title and the rest of
/// the URL as the subtitle.
#[component]
fn LinkBody(text: String) -> Element {
    let url = first_url(&text).unwrap_or(text.trim()).to_string();
    let note = (text.trim() != url).then(|| text.clone());
    let (domain, rest) = split_url(&url);

    rsx! {
        a {
            class: "item-card-link-main",
            href: "{url}",
            target: "_blank",
            rel: "noopener noreferrer",
            span { class: "item-card-link-row",
                span { class: "item-card-link-icon", IconLink {} }
                span { class: "item-card-link-body",
                    span { class: "item-card-link-title", "{domain}" }
                    if let Some(rest) = rest {
                        span { class: "item-card-link-path", "{rest}" }
                    }
                }
            }
        }
        if let Some(note) = note {
            p { class: "item-card-link-note", LinkedText { text: note } }
        }
    }
}

/// `text` with every URL in it a clickable link that opens in a new tab
/// (the system browser on desktop), like the link card itself.
#[component]
fn LinkedText(text: String) -> Element {
    rsx! {
        for segment in link_segments(&text) {
            match segment {
                Segment::Text(plain) => rsx! { "{plain}" },
                Segment::Url(url) => rsx! {
                    a {
                        class: "text-link",
                        href: "{url}",
                        target: "_blank",
                        rel: "noopener noreferrer",
                        // Only the link: not whatever the text sits in (e.g.
                        // a caption that opens the image viewer).
                        onclick: move |evt| evt.stop_propagation(),
                        "{url}"
                    }
                },
            }
        }
    }
}

/// Splits `https://www.example.com/a/b?q=1` into `("example.com",
/// Some("/a/b?q=1"))`. Hand-rolled rather than pulling a URL crate into
/// `ui` for display-only formatting; the backend has already validated that
/// link items are http(s) URLs.
fn split_url(url: &str) -> (String, Option<String>) {
    let without_scheme = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))
        .unwrap_or(url);
    let host_end = without_scheme
        .find(['/', '?', '#'])
        .unwrap_or(without_scheme.len());
    let (host, rest) = without_scheme.split_at(host_end);
    let host = host.strip_prefix("www.").unwrap_or(host);

    let rest = rest.trim_end_matches('/');
    let rest = (!rest.is_empty()).then(|| rest.to_string());
    (host.to_string(), rest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::i18n::tests::in_language;

    #[test]
    fn column_count_follows_available_width() {
        assert_eq!(column_count(200.0), 1);
        assert_eq!(column_count(499.0), 1);
        assert_eq!(column_count(500.0), 2);
        assert_eq!(column_count(759.0), 2);
        assert_eq!(column_count(760.0), 3);
        // 3 columns until they'd be wider than MAX_COLUMN_WIDTH_PX...
        assert_eq!(column_count(1360.0), 3);
        // ...then a 4th rather than stretching the cards.
        assert_eq!(column_count(1361.0), 4);
        assert_eq!(column_count(5000.0), 4);
    }

    #[test]
    fn upload_date_is_shown_in_the_viewers_time_zone() {
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let lisbon_summer = chrono::FixedOffset::east_opt(3600).unwrap();
        let new_york = chrono::FixedOffset::west_opt(4 * 3600).unwrap();

        // Pydantic's format: fractional seconds, "Z" for UTC.
        let date =
            UploadDate::in_zone("2026-09-24T12:21:14.658513Z", &utc, Language::English).unwrap();
        assert_eq!(date.label, "24 Sep 2026");
        assert_eq!(date.full, "24 September 2026, 12:21");

        // Just before midnight UTC is already the next day an hour east,
        // and just after is still the previous day four hours west.
        let late = "2026-09-24T23:30:00+00:00";
        assert_eq!(
            UploadDate::in_zone(late, &lisbon_summer, Language::English)
                .unwrap()
                .label,
            "25 Sep 2026"
        );
        let early = "2026-09-25T01:00:00Z";
        assert_eq!(
            UploadDate::in_zone(early, &new_york, Language::English)
                .unwrap()
                .label,
            "24 Sep 2026"
        );
    }

    #[test]
    fn upload_date_uses_the_ui_languages_month_names() {
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let date = UploadDate::in_zone("2026-09-24T12:21:14Z", &utc, Language::Ukrainian).unwrap();
        assert_eq!(date.label, "24 вер 2026");
        assert_eq!(date.full, "24 вересня 2026, 12:21");
    }

    #[test]
    fn unparseable_upload_date_is_omitted() {
        in_language(Language::English, || {
            assert!(UploadDate::from_api("not a date").is_none());
            assert!(UploadDate::from_api("").is_none());
        });
    }

    #[test]
    fn tag_names_are_compared_with_whitespace_collapsed() {
        assert_eq!(clean_tag_name("  machine   learning "), "machine learning");
        assert_eq!(clean_tag_name("   "), "");
    }

    #[test]
    fn file_details_show_type_and_readable_size() {
        // Without Fluent's invisible bidi isolation marks around values.
        let details = |language, filename: &str, size| {
            in_language(language, || file_details(filename, size))
                .replace(['\u{2068}', '\u{2069}'], "")
        };
        let english = Language::English;
        assert_eq!(details(english, "Report Q3.pdf", 1_258_291), "PDF · 1.2 MB");
        assert_eq!(details(english, "notes.md", 2_048), "MD · 2 KB");
        assert_eq!(details(english, "tiny.txt", 12), "TXT · 12 B");
        // No usable extension: size only.
        assert_eq!(details(english, "README", 12), "12 B");
        assert_eq!(details(english, ".bashrc", 12), "12 B");
        // The size in the UI language.
        assert_eq!(
            details(Language::Ukrainian, "Report Q3.pdf", 1_258_291),
            "PDF · 1,2 МБ"
        );
    }

    #[test]
    fn split_url_extracts_domain_and_rest() {
        assert_eq!(
            split_url("https://www.example.com/a/b?q=1"),
            ("example.com".to_string(), Some("/a/b?q=1".to_string()))
        );
        assert_eq!(
            split_url("http://example.com/"),
            ("example.com".to_string(), None)
        );
        assert_eq!(
            split_url("https://example.com"),
            ("example.com".to_string(), None)
        );
        assert_eq!(
            split_url("https://sub.example.com:8080?x"),
            ("sub.example.com:8080".to_string(), Some("?x".to_string()))
        );
    }
}
