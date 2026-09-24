use api::{ItemUpdate, ListedItem, Tag};
use chrono::{DateTime, Local, TimeZone};
use dioxus::prelude::*;
use futures_timer::Delay;

use crate::AuthSession;
use crate::filters::{TAG_LIST_LIMIT, TAG_SEARCH_DEBOUNCE};
use crate::icons::{
    IconClose, IconFile, IconHeart, IconHeartFilled, IconLink, IconMoreHorizontal, IconPencil,
    IconTrash,
};

const ITEMS_CSS: Asset = asset!("/assets/styling/items.css");
/// Tag chips, shared with the other tag UI (see tags.css).
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

/// Narrowest a column is allowed to get before the grid drops to one fewer
/// column. Measured against the grid's own width, not the viewport, so the
/// grid adapts to wherever it's placed.
const MIN_COLUMN_WIDTH_PX: f64 = 240.0;
const MAX_COLUMNS: usize = 3;
/// Must match `.item-grid`'s `gap` in items.css.
const COLUMN_GAP_PX: f64 = 20.0;

fn column_count(grid_width: f64) -> usize {
    // n columns fit when n * min + (n - 1) * gap <= width.
    let fitting = ((grid_width + COLUMN_GAP_PX) / (MIN_COLUMN_WIDTH_PX + COLUMN_GAP_PX)).floor();
    (fitting as usize).clamp(1, MAX_COLUMNS)
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
/// caller acts on them and refreshes `items` as needed.
#[component]
pub fn ItemGrid(
    items: Vec<ListedItem>,
    on_delete: EventHandler<String>,
    on_tags_changed: EventHandler<()>,
    on_favorite_changed: EventHandler<()>,
    on_edited: EventHandler<()>,
) -> Element {
    let mut columns = use_signal(|| MAX_COLUMNS);

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
    /// the next day. None if the timestamp can't be parsed.
    fn from_api(created_at: &str) -> Option<Self> {
        Self::in_zone(created_at, &Local)
    }

    fn in_zone<Tz: TimeZone>(created_at: &str, zone: &Tz) -> Option<Self>
    where
        Tz::Offset: std::fmt::Display,
    {
        let local = DateTime::parse_from_rfc3339(created_at)
            .ok()?
            .with_timezone(zone);
        Some(Self {
            label: local.format("%-d %b %Y").to_string(),
            full: local.format("%-d %B %Y, %H:%M").to_string(),
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

/// One saved item: its type-specific content, a footer with its tags and
/// upload date, and its actions menu.
///
/// The card is a plain box; only the content part is clickable (a link or
/// file opens, an image opens the viewer), so the tag controls in the
/// footer never trigger it. The menu sits in a wrapper *beside* the card,
/// and nothing in the card clips its content, so dropdowns can extend past
/// short cards.
///
/// Clicking an image opens it in an `ItemView` with the same controls as
/// the card; "Edit" in the menu opens any item there, in edit mode. The
/// card owns the view, so both show one favorite state and share the same
/// handlers.
///
/// `on_tags_changed` fires after a tag was added to or removed from it,
/// `on_favorite_changed` after its favorite state was saved, and
/// `on_edited` after an edit was saved.
#[component]
fn ItemCard(
    item: ListedItem,
    on_delete: EventHandler<String>,
    on_tags_changed: EventHandler<()>,
    on_favorite_changed: EventHandler<()>,
    on_edited: EventHandler<()>,
) -> Element {
    let session = use_context::<AuthSession>();
    // A saved edit's result, paired with the item it replaced: shown while
    // `item` is still that one, i.e. until the refetch `on_edited` asks for
    // arrives, so the old content doesn't flash back in the meantime.
    let mut edited = use_signal(|| None::<(ListedItem, ListedItem)>);
    let props_item = item;
    let item = match edited() {
        Some((before, after)) if before == props_item => after,
        _ => props_item.clone(),
    };
    // The user's latest choice, shown immediately (optimistically) while it
    // saves; None until they toggle, i.e. show the server's value.
    let mut favorite_override = use_signal(|| None::<bool>);
    let mut favorite_saving = use_signal(|| false);
    let is_favorite = favorite_override().unwrap_or(item.is_favorite);
    // Whether the item is open in its `ItemView`, and how.
    let mut view = use_signal(|| None::<ViewMode>);

    // Grid cards show the small thumbnail; the full original is only the
    // fallback while the thumbnail is still being generated. Viewing an
    // image opens the original.
    let image_url = item.thumbnail_url.as_ref().or(item.download_url.as_ref());
    let full_size_url = match item.r#type.as_str() {
        "image" => item.download_url.clone().or(item.thumbnail_url.clone()),
        _ => None,
    };
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
        ("link", _, Some(url)) => (
            "item-card-link",
            rsx! {
                LinkBody { url: url.clone() }
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
        (_, _, Some(text)) => (
            "item-card-note",
            rsx! {
                p { class: "item-card-note-text", "{text}" }
            },
        ),
        // e.g. an image whose download URL couldn't be produced.
        _ => return rsx! {},
    };

    let toggle_favorite = {
        let item_id = item.id.clone();
        move |_: ()| {
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
                    Ok(()) => on_favorite_changed.call(()),
                    // Didn't stick: show the real state again.
                    Err(_) => favorite_override.set(Some(previous)),
                }
                favorite_saving.set(false);
            });
        }
    };

    let item_id = item.id.clone();
    rsx! {
        div { class: if is_favorite { "item-card-shell is-favorite" } else { "item-card-shell" },
            div { class: "item-card {kind}",
                {body}
                div { class: "item-card-footer",
                    ItemTags {
                        item_id: item.id.clone(),
                        tags: item.tags.clone(),
                        on_changed: on_tags_changed,
                    }
                    CardDate { date: date.clone() }
                }
            }
            // Top-right, over the card: ♡ then ⋯.
            div { class: "item-card-actions",
                FavoriteButton { is_favorite, on_toggle: toggle_favorite.clone() }
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
                ItemView {
                    image_url: full_size_url,
                    editing: mode == ViewMode::Editing,
                    on_close: move |_| view.set(None),
                    on_cancel_edit: move |_| view.set(Some(ViewMode::Viewing)),
                    // The card's controls again, above the content.
                    div { class: "lightbox-panel-header",
                        CardDate { date }
                        div { class: "lightbox-actions",
                            FavoriteButton { is_favorite, on_toggle: toggle_favorite }
                            ItemMenu {
                                can_edit: mode == ViewMode::Viewing,
                                on_edit: move |_| view.set(Some(ViewMode::Editing)),
                                on_delete: move |_| {
                                    view.set(None);
                                    on_delete.call(item_id.clone());
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
                            }
                        },
                        // Tags are left out while editing: they save the
                        // moment they change, so Cancel couldn't undo them.
                        ViewMode::Editing => rsx! {
                            ItemEditor {
                                item: item.clone(),
                                on_cancel: move |_| view.set(Some(ViewMode::Viewing)),
                                on_saved: move |updated: Option<ListedItem>| {
                                    if let Some(updated) = updated {
                                        edited.set(Some((props_item.clone(), updated)));
                                        on_edited.call(());
                                    }
                                    view.set(Some(ViewMode::Viewing));
                                },
                            }
                        },
                    }
                }
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
        ("link", _, _, Some(url)) => rsx! {
            LinkBody { url }
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
            p { class: "lightbox-text", "{text}" }
        },
        _ => rsx! {},
    }
}

/// Form for the item's editable content, in its `ItemView`: a note's or
/// link's whole text, an image's caption, or a file's name and caption.
/// A caption can be added, changed or cleared. The server decides again
/// whether edited text is a note or a link. The text or caption field has
/// focus when the form opens.
///
/// Only changed fields are sent. `on_saved` gets the updated item, or None
/// if nothing had changed; `on_cancel` drops the changes.
#[component]
fn ItemEditor(
    item: ListedItem,
    on_cancel: EventHandler<()>,
    on_saved: EventHandler<Option<ListedItem>>,
) -> Element {
    let session = use_context::<AuthSession>();
    let original_text = item.text.clone().unwrap_or_default();
    let original_filename = item.file.as_ref().map(|file| file.filename.clone());
    let mut text = use_signal(|| original_text.clone());
    let mut filename = use_signal(|| original_filename.clone().unwrap_or_default());
    let mut saving = use_signal(|| false);
    let mut error = use_signal(|| None::<String>);

    let kind = item.r#type.as_str();
    let edits_text = matches!(kind, "text" | "link");
    let edits_filename = kind == "file";
    let edits_caption = matches!(kind, "image" | "file");

    let update = ItemUpdate {
        text: (edits_text || edits_caption)
            .then(|| text().trim().to_string())
            .filter(|text| text != original_text.trim()),
        filename: edits_filename
            .then(|| filename().trim().to_string())
            .filter(|name| Some(name) != original_filename.as_ref()),
    };
    let invalid = (edits_text && text().trim().is_empty())
        || (edits_filename && filename().trim().is_empty());

    let save = use_callback({
        let item_id = item.id.clone();
        move |()| {
            if saving() || invalid {
                return;
            }
            if update == ItemUpdate::default() {
                on_saved.call(None);
                return;
            }
            let session = session.clone();
            let item_id = item_id.clone();
            let update = update.clone();
            spawn(async move {
                saving.set(true);
                match session.update_item(item_id, update).await {
                    // This form closes; nothing more to reset here.
                    Ok(updated) => on_saved.call(Some(updated)),
                    Err(err) => {
                        error.set(Some(err.to_string()));
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
                    span { class: "item-editor-label", "Filename" }
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
                            "Text"
                        } else {
                            "Caption"
                        }
                    }
                    textarea {
                        class: if edits_text { "item-editor-input item-editor-textarea item-editor-textarea-tall" } else { "item-editor-input item-editor-textarea" },
                        placeholder: if edits_text { "" } else { "Add a caption" },
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
            if let Some(message) = error() {
                p { class: "item-editor-error", "{message}" }
            }
            div { class: "item-editor-buttons",
                button {
                    class: "item-editor-button item-editor-cancel",
                    r#type: "button",
                    disabled: saving(),
                    onclick: move |_| on_cancel.call(()),
                    "Cancel"
                }
                button {
                    class: "item-editor-button item-editor-save",
                    r#type: "button",
                    disabled: saving() || invalid,
                    onclick: move |_| save.call(()),
                    if saving() {
                        "Saving…"
                    } else {
                        "Save"
                    }
                }
            }
        }
    }
}

/// ♡ button: outlined, or filled while the item is a favorite.
#[component]
fn FavoriteButton(is_favorite: bool, on_toggle: EventHandler<()>) -> Element {
    let label = if is_favorite {
        "Remove from favorites"
    } else {
        "Add to favorites"
    };

    rsx! {
        button {
            class: if is_favorite { "item-favorite-button item-favorite-active" } else { "item-favorite-button" },
            r#type: "button",
            title: label,
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
                title: "More actions",
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
                            "Edit"
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
                        "Delete"
                    }
                }
            }
        }
    }
}

/// The item's tags as chips (× removes one), plus an "Add tag" control.
/// Changes go straight to the server; `on_changed` then lets the page
/// refetch, which also keeps tag filters honest (an item that loses a
/// filtered-by tag drops out of the results).
#[component]
fn ItemTags(item_id: String, tags: Vec<Tag>, on_changed: EventHandler<()>) -> Element {
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
                    Err(err) => error.set(Some(err.to_string())),
                }
                busy.set(false);
            });
        }
    };

    let assigned_names: Vec<String> = tags.iter().map(|tag| tag.name.clone()).collect();

    rsx! {
        div { class: "item-tags",
            for tag in tags {
                span { class: "tag-chip", key: "{tag.id}", title: "{tag.name}",
                    span { class: "tag-chip-name", "{tag.name}" }
                    button {
                        class: "tag-chip-remove",
                        r#type: "button",
                        title: "Remove tag",
                        aria_label: "Remove tag {tag.name}",
                        disabled: busy(),
                        onclick: {
                            let session = session.clone();
                            let item_id = item_id.clone();
                            move |_| {
                                let session = session.clone();
                                let item_id = item_id.clone();
                                let tag_id = tag.id.clone();
                                spawn(async move {
                                    busy.set(true);
                                    match session.remove_tag(item_id, tag_id).await {
                                        Ok(()) => {
                                            error.set(None);
                                            on_changed.call(());
                                        }
                                        Err(err) => error.set(Some(err.to_string())),
                                    }
                                    busy.set(false);
                                });
                            }
                        },
                        IconClose {}
                    }
                }
            }
            if adding() {
                TagPicker {
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
                    "+ Add tag"
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

/// Input for choosing a tag to add: existing tags containing the typed text
/// are listed below it (minus `exclude`: names already chosen, compared
/// case-insensitively); picking one, or "Create …" when nothing matches
/// exactly, calls `on_pick` with the name. Enter picks the exact match or
/// creates (and never submits a surrounding form); Escape or a click
/// outside closes it. Used on item cards and in the capture box.
#[component]
pub(crate) fn TagPicker(
    exclude: Vec<String>,
    busy: bool,
    on_pick: EventHandler<String>,
    on_close: EventHandler<()>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut query = use_signal(String::new);

    let suggestions = use_resource(move || {
        let session = session.clone();
        let query = clean_tag_name(&query());
        async move {
            if !query.is_empty() {
                Delay::new(TAG_SEARCH_DEBOUNCE).await;
            }
            session.list_tags(query, TAG_LIST_LIMIT).await
        }
    });

    let typed = clean_tag_name(&query());
    let excluded: Vec<String> = exclude.iter().map(|name| name.to_lowercase()).collect();
    let (found, exact_exists) = match &*suggestions.read() {
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
                placeholder: "Tag name",
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
                            "Create “{typed}”"
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
            img { src: "{url}", alt: "Saved image", loading: "lazy" }
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
#[component]
fn ItemView(
    image_url: Option<String>,
    editing: bool,
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
            class: "lightbox",
            role: "dialog",
            aria_modal: "true",
            aria_label: if has_image { "Image viewer" } else { "Item" },
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
                            alt: "Saved image",
                        }
                    }
                }
                div { class: "lightbox-panel", {children} }
            }
            button {
                class: "lightbox-close",
                r#type: "button",
                title: "Close",
                aria_label: "Close",
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

/// "PDF · 1.2 MB": the extension as a type label, plus a readable size.
fn file_details(filename: &str, size_bytes: u64) -> String {
    let size = format_size(size_bytes);
    match filename.rsplit_once('.') {
        Some((stem, extension)) if !stem.is_empty() && !extension.is_empty() => {
            format!("{} · {size}", extension.to_uppercase())
        }
        _ => size,
    }
}

fn format_size(bytes: u64) -> String {
    const KB: f64 = 1024.0;
    const MB: f64 = KB * 1024.0;
    let bytes_f = bytes as f64;
    if bytes_f >= MB {
        format!("{:.1} MB", bytes_f / MB)
    } else if bytes_f >= KB {
        format!("{:.0} KB", bytes_f / KB)
    } else {
        format!("{bytes} B")
    }
}

/// A saved link. The API only stores the URL itself (no page title or
/// preview image yet), so the domain stands in as the title and the rest of
/// the URL as the subtitle.
#[component]
fn LinkBody(url: String) -> Element {
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

    #[test]
    fn column_count_follows_available_width() {
        assert_eq!(column_count(200.0), 1);
        assert_eq!(column_count(499.0), 1);
        assert_eq!(column_count(500.0), 2);
        assert_eq!(column_count(759.0), 2);
        assert_eq!(column_count(760.0), 3);
        assert_eq!(column_count(5000.0), 3);
    }

    #[test]
    fn upload_date_is_shown_in_the_viewers_time_zone() {
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let lisbon_summer = chrono::FixedOffset::east_opt(3600).unwrap();
        let new_york = chrono::FixedOffset::west_opt(4 * 3600).unwrap();

        // Pydantic's format: fractional seconds, "Z" for UTC.
        let date = UploadDate::in_zone("2026-09-24T12:21:14.658513Z", &utc).unwrap();
        assert_eq!(date.label, "24 Sep 2026");
        assert_eq!(date.full, "24 September 2026, 12:21");

        // Just before midnight UTC is already the next day an hour east,
        // and just after is still the previous day four hours west.
        let late = "2026-09-24T23:30:00+00:00";
        assert_eq!(
            UploadDate::in_zone(late, &lisbon_summer).unwrap().label,
            "25 Sep 2026"
        );
        let early = "2026-09-25T01:00:00Z";
        assert_eq!(
            UploadDate::in_zone(early, &new_york).unwrap().label,
            "24 Sep 2026"
        );
    }

    #[test]
    fn unparseable_upload_date_is_omitted() {
        assert!(UploadDate::from_api("not a date").is_none());
        assert!(UploadDate::from_api("").is_none());
    }

    #[test]
    fn tag_names_are_compared_with_whitespace_collapsed() {
        assert_eq!(clean_tag_name("  machine   learning "), "machine learning");
        assert_eq!(clean_tag_name("   "), "");
    }

    #[test]
    fn file_details_show_type_and_readable_size() {
        assert_eq!(file_details("Report Q3.pdf", 1_258_291), "PDF · 1.2 MB");
        assert_eq!(file_details("notes.md", 2_048), "MD · 2 KB");
        assert_eq!(file_details("tiny.txt", 12), "TXT · 12 B");
        // No usable extension: size only.
        assert_eq!(file_details("README", 12), "12 B");
        assert_eq!(file_details(".bashrc", 12), "12 B");
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
