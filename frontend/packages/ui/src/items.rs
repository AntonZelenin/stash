use api::{Collection, ItemUpdate, ListedItem, Tag, TextItemType};
use chrono::{DateTime, Local, NaiveDate, TimeZone};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::collections::{
    COLLECTIONS_CSS, CollectionChip, CollectionPicker, contains_name, toggled,
};
use crate::filters::{TAG_LIST_LIMIT, TAG_SEARCH_DEBOUNCE};
use crate::i18n::{Language, api_error_message, current_language};
use crate::icons::{
    IconChevronLeft, IconChevronRight, IconClose, IconFile, IconHeart, IconHeartFilled, IconLink,
    IconMoreHorizontal, IconPencil, IconTrash,
};
use crate::media::{AudioBody, AudioStage, MediaKind, MediaPlayer, VideoBody};
use crate::text_kind::{Segment, TextKind, first_url, link_segments, text_kind};
use crate::viewer_nav::{Step, ViewerNav, keep_step_keys, step_for_key, stepped};

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
/// The grid also knows which item is open in its viewer, so the viewer can
/// step to the previous or next item of `items`, in their order: the
/// result set the item was opened from, whatever the types.
///
/// With `group_by_day` (chronological listings), the items are split into
/// one section per day saved, each headed by the day and laid out as its own
/// masonry grid; `items` must already be in the order to show, and each
/// day keeps it. The viewer still steps through all of `items`.
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
    #[props(default)] group_by_day: bool,
    on_delete: EventHandler<String>,
    on_tags_changed: EventHandler<()>,
    on_favorite_changed: EventHandler<()>,
    on_edited: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
) -> Element {
    let mut columns = use_signal(|| PREFERRED_MAX_COLUMNS);
    let mut open = use_signal(|| None::<OpenItem>);

    // The result set as shown: items without a card are left out, so
    // stepping never lands on one.
    let items: Vec<ListedItem> = items.into_iter().filter(has_card).collect();
    let ids: Vec<String> = items.iter().map(|item| item.id.clone()).collect();

    // An item that left the set (deleted, or no longer matching the filters)
    // closes; forgotten, so it doesn't reopen should it come back.
    use_effect(use_reactive!(|ids| {
        if open
            .peek()
            .as_ref()
            .is_some_and(|open| !ids.contains(&open.id))
        {
            open.set(None);
        }
    }));

    let on_view = use_callback(move |(id, mode): (String, Option<ViewMode>)| {
        open.set(mode.map(|mode| OpenItem {
            id,
            mode,
            arrived_by: None,
        }));
    });
    // Reads the open item when called, not when rendered, so presses
    // quicker than a render each count from the item the last one opened.
    let on_step = use_callback({
        let ids = ids.clone();
        move |step: Step| {
            let next = open().and_then(|open| open.stepped(&ids, step));
            if next.is_some() {
                open.set(next);
            }
        }
    });

    let opened = open().filter(|open| ids.contains(&open.id));
    let nav = opened.as_ref().and_then(|open| {
        let preload = neighbour_images(&items, &open.id);
        ViewerNav::new(&ids, &open.id, open.arrived_by, preload, on_step)
    });

    let current_columns = columns();
    let sections = if group_by_day {
        day_sections(items, &Local, Local::now().date_naive())
    } else {
        vec![(None, items)]
    };

    rsx! {
        document::Link { rel: "stylesheet", href: ITEMS_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }
        document::Link { rel: "stylesheet", href: COLLECTIONS_CSS }

        // Measured here rather than on each section's grid: every section
        // has the same width, so the same column count.
        div {
            class: "item-feed",
            onresize: move |evt| {
                if let Ok(size) = evt.get_content_box_size() {
                    let count = column_count(size.width);
                    if count != columns() {
                        columns.set(count);
                    }
                }
            },
            for (heading , section_items) in sections {
                section {
                    class: "item-feed-section",
                    key: "{heading.map(DayHeading::key).unwrap_or_default()}",
                    if let Some(heading) = heading {
                        h2 { class: "item-feed-heading", {heading.label()} }
                    }
                    div { class: "item-grid",
                        for (index , stack) in masonry_stacks(section_items, current_columns).into_iter().enumerate() {
                            div { class: "item-grid-column", key: "{index}",
                                for item in stack {
                                    ItemCard {
                                        key: "{item.id}",
                                        opened: opened.as_ref().filter(|open| open.id == item.id).map(|open| open.mode),
                                        nav: nav.clone().filter(|_| opened.as_ref().is_some_and(|open| open.id == item.id)),
                                        on_view,
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
        }
    }
}

/// `items` dealt round-robin into `columns` stacks (see `ItemGrid`).
fn masonry_stacks(items: Vec<ListedItem>, columns: usize) -> Vec<Vec<ListedItem>> {
    let mut stacks: Vec<Vec<ListedItem>> = vec![Vec::new(); columns];
    for (index, item) in items.into_iter().enumerate() {
        stacks[index % columns].push(item);
    }
    stacks
}

/// The heading of a day's section in a chronological listing.
#[derive(Clone, Copy, Debug, PartialEq)]
enum DayHeading {
    Today,
    Yesterday,
    /// Any other day, shown as its date.
    Date(NaiveDate),
}

impl DayHeading {
    fn new(day: NaiveDate, today: NaiveDate) -> Self {
        if day == today {
            DayHeading::Today
        } else if today.pred_opt() == Some(day) {
            DayHeading::Yesterday
        } else {
            DayHeading::Date(day)
        }
    }

    /// A date reads "26 вересня 2026": the month in the genitive in
    /// Ukrainian.
    fn label(self) -> String {
        match self {
            DayHeading::Today => t!("items-today"),
            DayHeading::Yesterday => t!("items-yesterday"),
            DayHeading::Date(day) => day
                .format_localized("%-d %B %Y", current_language().chrono_locale())
                .to_string(),
        }
    }

    /// Distinct per day, for the section's key.
    fn key(self) -> String {
        match self {
            DayHeading::Today => "today".to_string(),
            DayHeading::Yesterday => "yesterday".to_string(),
            DayHeading::Date(day) => day.to_string(),
        }
    }
}

/// `items` split by the day (in `zone`) each was saved: the days in the
/// order their first item appears, each day's items in their order, so a
/// sorted list stays sorted. Items whose timestamp can't be parsed go into
/// a last section without a heading.
fn day_sections<Tz: TimeZone>(
    items: Vec<ListedItem>,
    zone: &Tz,
    today: NaiveDate,
) -> Vec<(Option<DayHeading>, Vec<ListedItem>)> {
    let mut days: Vec<(NaiveDate, Vec<ListedItem>)> = Vec::new();
    let mut undated = Vec::new();
    for item in items {
        let day = DateTime::parse_from_rfc3339(&item.created_at)
            .ok()
            .map(|at| at.with_timezone(zone).date_naive());
        match day {
            Some(day) => match days.iter_mut().find(|(existing, _)| *existing == day) {
                Some((_, day_items)) => day_items.push(item),
                None => days.push((day, vec![item])),
            },
            None => undated.push(item),
        }
    }
    let mut sections: Vec<_> = days
        .into_iter()
        .map(|(day, items)| (Some(DayHeading::new(day, today)), items))
        .collect();
    if !undated.is_empty() {
        sections.push((None, undated));
    }
    sections
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
#[derive(Clone, Copy, Debug, PartialEq)]
enum ViewMode {
    Viewing,
    Editing,
}

/// The item open in an `ItemGrid`'s viewer.
#[derive(Clone, Debug, PartialEq)]
struct OpenItem {
    id: String,
    mode: ViewMode,
    /// Set when it was reached by stepping from another item.
    arrived_by: Option<Step>,
}

impl OpenItem {
    /// The item `step` away in `ids`, open for viewing; None at either end,
    /// while editing (the changes would be lost), or if this one has left
    /// the set.
    fn stepped(&self, ids: &[String], step: Step) -> Option<Self> {
        if self.mode == ViewMode::Editing {
            return None;
        }
        stepped(ids, &self.id, step).map(|id| Self {
            id: id.clone(),
            mode: ViewMode::Viewing,
            arrived_by: Some(step),
        })
    }
}

/// Whether `ItemCard` shows anything for `item`: mirrors its content
/// match (e.g. an image whose URL couldn't be produced, and without a
/// caption, has no card).
fn has_card(item: &ListedItem) -> bool {
    let has_image = item.thumbnail_url.is_some() || item.download_url.is_some();
    match (item.r#type.as_str(), &item.file) {
        ("image", _) if has_image => true,
        ("file", Some(file)) => file.is_video() || file.is_audio() || item.download_url.is_some(),
        ("file", None) => false,
        _ => item.text.is_some(),
    }
}

/// The full-size image an image item's viewer shows (not the thumbnail,
/// which is only the fallback while it's being generated).
fn viewer_image_url(item: &ListedItem) -> Option<String> {
    (item.r#type == "image")
        .then(|| item.download_url.clone().or(item.thumbnail_url.clone()))
        .flatten()
}

/// The viewer images of the items either side of `current` in `items`,
/// to fetch ahead of stepping there. Only images: players fetch their own
/// URL when they open, and the rest is already loaded with the list.
fn neighbour_images(items: &[ListedItem], current: &str) -> Vec<String> {
    let ids: Vec<String> = items.iter().map(|item| item.id.clone()).collect();
    [Step::Previous, Step::Next]
        .into_iter()
        .filter_map(|step| stepped(&ids, current, step))
        .filter_map(|id| items.iter().find(|item| item.id == *id))
        .filter_map(viewer_image_url)
        .collect()
}

/// One saved item: its type-specific content, a footer with its tags,
/// favorite button and upload date, and its actions menu.
///
/// The card is a plain box; only the content part is clickable (a link or
/// file opens, an image or video opens the viewer), so the tag controls in the
/// footer never trigger it. The menu sits in a wrapper *beside* the card,
/// and nothing in the card clips its content, so dropdowns can extend past
/// short cards.
///
/// Clicking an image or video opens it in an `ItemView` with the same controls as
/// the card, as does clicking a note's (clamped) preview, to read the whole
/// text; "Edit" in the menu opens any item there, in edit mode. The
/// card owns the view, so both show one favorite state and share the same
/// handlers.
///
/// Whether (and how) it's open is kept by the grid: `opened`, and `nav` to
/// step to its neighbours from there. `on_view` asks the grid to open it
/// (in a mode) or close it (None).
///
/// `on_tags_changed` fires after a tag was added to or removed from it,
/// `on_favorite_changed` after its favorite state was saved, and
/// `on_edited` after an edit was saved, and `on_tag_click` with a tag
/// clicked on the card or in its view (which then closes).
#[component]
fn ItemCard(
    item: ListedItem,
    opened: Option<ViewMode>,
    nav: Option<ViewerNav>,
    on_view: Callback<(String, Option<ViewMode>)>,
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
    // Opens the item in its `ItemView` (or closes it: None).
    let view = use_callback({
        let item_id = item.id.clone();
        move |mode: Option<ViewMode>| on_view.call((item_id.clone(), mode))
    });

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
                    on_open: move |_| view.call(Some(ViewMode::Viewing)),
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
            // Previewed like an image; its viewer fetches its own URL.
            (Some(file), _) if file.is_video() => (
                "item-card-media item-card-video",
                rsx! {
                    VideoBody {
                        thumbnail_url: item.thumbnail_url.clone(),
                        video_url: item.download_url.clone(),
                        filename: file.filename.clone(),
                        details: file_details(&file.filename, file.size_bytes),
                        caption: caption.clone(),
                        on_open: move |_| view.call(Some(ViewMode::Viewing)),
                    }
                },
            ),
            // Opens its player, like a video.
            (Some(file), _) if file.is_audio() => (
                "item-card-file item-card-media item-card-audio",
                rsx! {
                    AudioBody {
                        item_id: item.id.clone(),
                        audio_url: item.download_url.clone(),
                        filename: file.filename.clone(),
                        details: file_details(&file.filename, file.size_bytes),
                        caption: caption.clone(),
                        on_open: move |_| view.call(Some(ViewMode::Viewing)),
                    }
                },
            ),
            (Some(file), Some(url)) => (
                if file.is_media() {
                    "item-card-file item-card-media"
                } else {
                    "item-card-file"
                },
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
                    onclick: move |_| view.call(Some(ViewMode::Viewing)),
                    onkeydown: move |evt| {
                        if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                            evt.prevent_default();
                            view.call(Some(ViewMode::Viewing));
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
                    on_edit: move |_| view.call(Some(ViewMode::Editing)),
                    on_delete: {
                        let item_id = item_id.clone();
                        move |_| on_delete.call(item_id.clone())
                    },
                }
            }
            if let Some(mode) = opened {
                OpenedItem {
                    item: item.clone(),
                    mode,
                    nav,
                    is_favorite,
                    on_toggle_favorite: toggle_favorite,
                    on_mode: move |mode| view.call(mode),
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
    /// Stepping to the neighbouring items, when opened from a result set.
    #[props(default)]
    nav: Option<ViewerNav>,
    is_favorite: bool,
    on_toggle_favorite: EventHandler<()>,
    on_mode: EventHandler<Option<ViewMode>>,
    on_delete: EventHandler<()>,
    on_tags_changed: EventHandler<()>,
    on_tag_click: EventHandler<Tag>,
    on_saved: EventHandler<ListedItem>,
) -> Element {
    let media = match (item.r#type.as_str(), &item.file) {
        ("image", _) => viewer_image_url(&item).map(ViewMedia::Image),
        ("file", Some(file)) if file.is_video() => Some(ViewMedia::Video {
            item_id: item.id.clone(),
            poster: item.thumbnail_url.clone(),
        }),
        ("file", Some(file)) if file.is_audio() => Some(ViewMedia::Audio {
            item_id: item.id.clone(),
            title: file.filename.clone(),
        }),
        _ => None,
    };
    let is_note = !matches!(item.r#type.as_str(), "image" | "link" | "file");

    rsx! {
        ItemView {
            media,
            nav,
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
                    if !item.collections.is_empty() {
                        div { class: "item-collections",
                            for collection in item.collections.clone() {
                                CollectionChip { key: "{collection.id}", name: collection.name }
                            }
                        }
                    }
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
/// opens. Below it, the collections the item is in and its tags, each with
/// a × to remove it, and "+ Collection" (a multi-select picker) and "Add
/// tag" controls; these changes are only applied on Save, so Cancel undoes
/// them too.
///
/// Only changed fields are sent. `on_saved` gets the updated item, or None
/// if nothing had changed; `on_cancel` drops the changes. If a collection
/// or tag change fails, the form stays open and `on_tags_changed` lets the
/// page refetch, as the changes before it were applied.
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
    // Likewise for collections: ids of the ones the item is taken out of,
    // and names of the ones it's put in.
    let mut removed_collection_ids = use_signal(Vec::<String>::new);
    let mut added_collection_names = use_signal(Vec::<String>::new);
    let mut picking_collection = use_signal(|| false);
    let kept_collections: Vec<Collection> = item
        .collections
        .iter()
        .filter(|collection| !removed_collection_ids.read().contains(&collection.id))
        .cloned()
        .collect();
    let shown_collection_names: Vec<String> = kept_collections
        .iter()
        .map(|collection| collection.name.clone())
        .chain(added_collection_names())
        .collect();
    // Checking a collection in the picker puts the item in it, unchecking
    // takes it out; either way back to what it was just undoes the change.
    let toggle_collection = {
        let collections = item.collections.clone();
        let shown = shown_collection_names.clone();
        move |name: String| {
            let lowercase = name.to_lowercase();
            let original = collections
                .iter()
                .find(|collection| collection.name.to_lowercase() == lowercase);
            match (original, contains_name(&shown, &name)) {
                (Some(collection), true) => {
                    removed_collection_ids.write().push(collection.id.clone())
                }
                (Some(collection), false) => removed_collection_ids
                    .write()
                    .retain(|id| *id != collection.id),
                (None, _) => added_collection_names.set(toggled(&added_collection_names(), &name)),
            }
        }
    };
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
        let kept_collections = kept_collections.clone();
        move |()| {
            if saving() || invalid {
                return;
            }
            let removed = removed_tag_ids();
            let added = added_tag_names();
            let left_collections = removed_collection_ids();
            let joined_collections = added_collection_names();
            if update == ItemUpdate::default()
                && removed.is_empty()
                && added.is_empty()
                && left_collections.is_empty()
                && joined_collections.is_empty()
            {
                on_saved.call(None);
                return;
            }
            let session = session.clone();
            let item = item.clone();
            let kept_tags = kept_tags.clone();
            let kept_collections = kept_collections.clone();
            let update = update.clone();
            spawn(async move {
                saving.set(true);
                // Collections and tags first, so an updated item from the
                // server below already has them right. Every call is a
                // no-op when repeated, so saving again after a failure is
                // safe.
                let mut collections = kept_collections;
                let mut tags = kept_tags;
                let mut failure = None;
                for collection_id in left_collections {
                    if let Err(err) = session
                        .remove_from_collection(item.id.clone(), collection_id)
                        .await
                    {
                        failure = Some(err);
                        break;
                    }
                }
                if failure.is_none() {
                    for name in joined_collections {
                        match session.add_to_collection(item.id.clone(), name).await {
                            Ok(collection) => collections.push(collection),
                            Err(err) => {
                                failure = Some(err);
                                break;
                            }
                        }
                    }
                }
                if failure.is_none() {
                    for tag_id in removed {
                        if let Err(err) = session.remove_tag(item.id.clone(), tag_id).await {
                            failure = Some(err);
                            break;
                        }
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
                    // Only collections or tags changed: the item as it now
                    // is, both sorted by name as the server lists them.
                    collections.sort_by(|a, b| a.name.cmp(&b.name));
                    tags.sort_by_key(|tag| tag.name.to_lowercase());
                    on_saved.call(Some(ListedItem {
                        tags,
                        collections,
                        ..item
                    }));
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
                span { class: "item-editor-label", {t!("item-field-collections")} }
                // The anchor is the whole row, not the button: picking a
                // collection adds a chip before the button, which would
                // move an open picker hung from it.
                div { class: "item-editor-tags collection-picker-anchor",
                    for collection in kept_collections {
                        CollectionChip {
                            key: "{collection.id}",
                            name: collection.name.clone(),
                            disabled: saving(),
                            on_remove: move |_| removed_collection_ids.write().push(collection.id.clone()),
                        }
                    }
                    for name in added_collection_names() {
                        CollectionChip {
                            key: "added-{name}",
                            name: name.clone(),
                            disabled: saving(),
                            on_remove: move |_| {
                                added_collection_names.set(toggled(&added_collection_names(), &name));
                            },
                        }
                    }
                    button {
                        class: "tag-add",
                        r#type: "button",
                        title: t!("collections-add-title"),
                        aria_haspopup: "dialog",
                        aria_expanded: if picking_collection() { "true" } else { "false" },
                        disabled: saving(),
                        onclick: move |_| picking_collection.toggle(),
                        "+ "
                        {t!("collections-add")}
                    }
                    if picking_collection() {
                        CollectionPicker {
                            selected: shown_collection_names,
                            busy: saving(),
                            on_toggle: toggle_collection,
                            on_close: move |_| picking_collection.set(false),
                        }
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
                        // ← → move the caret, not a viewer it's in.
                        keep_step_keys(&evt);
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

/// What an `ItemView` shows beside its panel.
#[derive(Clone, PartialEq)]
enum ViewMedia {
    /// The image at this URL.
    Image(String),
    /// The video item's player (it fetches its own URL); `poster` is its
    /// thumbnail, if any.
    Video {
        item_id: String,
        poster: Option<String>,
    },
    /// The audio item's player, under its `title` (its filename).
    Audio { item_id: String, title: String },
}

/// An item opened over the whole page, on a dimmed backdrop: a panel with
/// `children` (the item's content and controls) and, for an image, video
/// or audio file, the `media` itself (scaled down to fit the screen if
/// needed; an image never up) with the panel beside it on wide screens and
/// below it on narrow ones.
///
/// The ✕ button, Escape and a click anywhere on the backdrop close it.
/// While `editing`, Escape calls `on_cancel_edit` instead (like the form's
/// Cancel button), and backdrop clicks are ignored so a stray click doesn't
/// throw the changes away.
///
/// `reading` (a note being read) widens the panel for its text and lets it
/// grow to the text's full length: the backdrop then scrolls, rather than
/// the text inside the panel.
///
/// With `nav` (opened from a result set), ← → and arrow buttons at the
/// screen's sides step to the previous/next item; not while `editing`.
/// Text fields and players inside keep ← → for themselves (see
/// `keep_step_keys`).
#[component]
fn ItemView(
    media: Option<ViewMedia>,
    #[props(default)] nav: Option<ViewerNav>,
    editing: bool,
    #[props(default)] reading: bool,
    on_close: EventHandler<()>,
    on_cancel_edit: EventHandler<()>,
    children: Element,
) -> Element {
    let has_media = media.is_some();
    let label = match media {
        Some(ViewMedia::Image(_)) => t!("item-image-viewer"),
        Some(ViewMedia::Video { .. }) => t!("item-video-viewer"),
        Some(ViewMedia::Audio { .. }) => t!("item-audio-viewer"),
        None => t!("item-viewer"),
    };
    let mut dialog = use_signal(|| None::<std::rc::Rc<MountedData>>);
    // The step that opened this item, if any (each item stepped to gets a
    // view of its own), and its button in this view.
    let arrived_by = use_hook(|| nav.as_ref().and_then(|nav| nav.arrived_by));
    let mut arrival_button = use_signal(|| None::<std::rc::Rc<MountedData>>);
    let mut arrival_focused = use_signal(|| false);
    let nav = nav.filter(|_| !editing);

    // Keep keyboard focus on the dialog whenever it's not being edited, so
    // Escape and ← → reach it: on opening, and after leaving edit mode, when
    // the focused form field (or Cancel/Save button) has just been removed.
    // While editing, the form's field has focus instead. Reached with an
    // arrow button (or key), that button has it at first instead, so
    // pressing it again keeps going; ← → still work from there.
    use_effect(use_reactive!(|editing| {
        if editing {
            return;
        }
        let target = if arrived_by.is_some() && !*arrival_focused.peek() {
            let button = arrival_button();
            if button.is_some() {
                arrival_focused.set(true);
            }
            button
        } else {
            dialog()
        };
        if let Some(target) = target {
            spawn(async move {
                let _ = target.set_focus(true).await;
            });
        }
    }));

    let has_nav = nav.is_some();
    let key_nav = nav.clone();
    rsx! {
        div {
            class: match (reading, has_nav) {
                (false, false) => "lightbox",
                (false, true) => "lightbox lightbox-has-nav",
                (true, false) => "lightbox lightbox-reading",
                (true, true) => "lightbox lightbox-reading lightbox-has-nav",
            },
            role: "dialog",
            aria_modal: "true",
            aria_label: label,
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
                } else if let Some(nav) = &key_nav
                    && let Some(step) = step_for_key(&evt.key(), evt.modifiers(), editing)
                {
                    // Not also scrolling the backdrop sideways.
                    evt.prevent_default();
                    if nav.allows(step) {
                        nav.on_step.call(step);
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
                class: if has_media { "lightbox-content" } else { "lightbox-content lightbox-content-no-media" },
                // Also catches clicks on the full-screen backdrops of the
                // menu and tag picker inside, which only close those.
                onclick: move |evt| evt.stop_propagation(),
                match media {
                    Some(ViewMedia::Image(url)) => rsx! {
                        div { class: "lightbox-media",
                            img {
                                class: "lightbox-image",
                                src: "{url}",
                                alt: t!("item-image-alt"),
                            }
                        }
                    },
                    Some(ViewMedia::Video { item_id, poster }) => rsx! {
                        div { class: "lightbox-media",
                            MediaPlayer { item_id, kind: MediaKind::Video, poster }
                        }
                    },
                    Some(ViewMedia::Audio { item_id, title }) => rsx! {
                        div { class: "lightbox-media lightbox-media-audio",
                            AudioStage { item_id, title }
                        }
                    },
                    None => rsx! {},
                }
                div { class: "lightbox-panel", {children} }
            }
            if let Some(nav) = nav {
                // `aria-disabled` at either end rather than `disabled`: a
                // disabled button can't keep focus (having just stepped to
                // the last item with it), and in some browsers a click on
                // one falls through to the backdrop, closing the view.
                for step in [Step::Previous, Step::Next] {
                    button {
                        key: "{step:?}",
                        class: match step {
                            Step::Previous => "lightbox-nav lightbox-nav-previous",
                            Step::Next => "lightbox-nav lightbox-nav-next",
                        },
                        r#type: "button",
                        title: step.label(),
                        aria_label: step.label(),
                        aria_disabled: if nav.allows(step) { "false" } else { "true" },
                        onmounted: move |evt| {
                            if arrived_by == Some(step) {
                                arrival_button.set(Some(evt.data()));
                            }
                        },
                        onclick: {
                            let nav = nav.clone();
                            move |evt: MouseEvent| {
                                evt.stop_propagation();
                                if nav.allows(step) {
                                    nav.on_step.call(step);
                                }
                            }
                        },
                        match step {
                            Step::Previous => rsx! { IconChevronLeft {} },
                            Step::Next => rsx! { IconChevronRight {} },
                        }
                    }
                }
                for url in nav.preload.clone() {
                    img {
                        key: "{url}",
                        class: "lightbox-preload",
                        src: "{url}",
                        alt: "",
                        aria_hidden: "true",
                    }
                }
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

    fn item(id: &str, r#type: &str) -> ListedItem {
        ListedItem {
            id: id.to_string(),
            r#type: r#type.to_string(),
            status: "ready".to_string(),
            created_at: "2026-09-24T12:21:14Z".to_string(),
            text: None,
            download_url: None,
            thumbnail_url: None,
            file: None,
            tags: Vec::new(),
            collections: Vec::new(),
            is_favorite: false,
        }
    }

    fn image(id: &str) -> ListedItem {
        ListedItem {
            download_url: Some(format!("https://files/{id}.png")),
            thumbnail_url: Some(format!("https://files/{id}.webp")),
            ..item(id, "image")
        }
    }

    fn file(id: &str, kind: &str, download_url: bool) -> ListedItem {
        ListedItem {
            download_url: download_url.then(|| format!("https://files/{id}")),
            file: Some(api::ListedFile {
                filename: format!("{id}.bin"),
                content_type: "application/octet-stream".to_string(),
                size_bytes: 1,
                kind: kind.to_string(),
            }),
            ..item(id, "file")
        }
    }

    fn note(id: &str, r#type: &str) -> ListedItem {
        ListedItem {
            text: Some(format!("{id} https://example.com")),
            ..item(id, r#type)
        }
    }

    /// The grid's result set: `items` as given, minus those without a card.
    fn result_set(items: &[ListedItem]) -> Vec<String> {
        items
            .iter()
            .filter(|item| has_card(item))
            .map(|item| item.id.clone())
            .collect()
    }

    fn viewing(id: &str) -> OpenItem {
        OpenItem {
            id: id.to_string(),
            mode: ViewMode::Viewing,
            arrived_by: None,
        }
    }

    /// Where `steps`, applied one after another from `start`, end up.
    fn after(ids: &[String], start: OpenItem, steps: &[Step]) -> OpenItem {
        steps
            .iter()
            .fold(start, |open, &step| open.stepped(ids, step).unwrap_or(open))
    }

    #[test]
    fn stepping_crosses_item_types() {
        // image → PDF → note → link → video → audio
        let items = [
            image("img"),
            file("pdf", "document", true),
            note("txt", "text"),
            note("url", "link"),
            file("vid", "video", false),
            file("mp3", "audio", false),
        ];
        let ids = result_set(&items);
        assert_eq!(ids, ["img", "pdf", "txt", "url", "vid", "mp3"]);

        let next = viewing("img").stepped(&ids, Step::Next).unwrap();
        assert_eq!(next.id, "pdf");
        assert_eq!(next.mode, ViewMode::Viewing);
        assert_eq!(next.arrived_by, Some(Step::Next));
        assert_eq!(
            after(&ids, viewing("img"), &[Step::Next; 5]).id,
            "mp3",
            "every type in turn"
        );
        let back = viewing("url").stepped(&ids, Step::Previous).unwrap();
        assert_eq!(
            (back.id.as_str(), back.arrived_by),
            ("txt", Some(Step::Previous))
        );
    }

    #[test]
    fn stepping_keeps_the_result_sets_order() {
        // e.g. search results, ranked by relevance rather than date or type.
        let items = [note("b", "text"), image("c"), note("a", "link")];
        let ids = result_set(&items);
        assert_eq!(
            after(&ids, viewing("b"), &[Step::Next]).id,
            "c",
            "the next result, not the next by id"
        );
        assert_eq!(after(&ids, viewing("c"), &[Step::Next]).id, "a");
    }

    #[test]
    fn stepping_skips_items_without_a_card() {
        let unreachable_image = item("broken", "image");
        let file_without_url = file("gone", "document", false);
        let items = [
            note("a", "text"),
            unreachable_image,
            file_without_url,
            note("b", "text"),
        ];
        assert_eq!(result_set(&items), ["a", "b"]);
    }

    #[test]
    fn processing_or_failed_items_are_stepped_to_like_any_other() {
        // Their viewer shows what there is: the original until a thumbnail
        // is made, or the player's own loading/failed message.
        let processing = ListedItem {
            thumbnail_url: None,
            status: "processing".to_string(),
            ..image("new")
        };
        let failed_video = ListedItem {
            status: "failed".to_string(),
            ..file("vid", "video", false)
        };
        let ids = result_set(&[note("a", "text"), processing, failed_video]);
        assert_eq!(ids, ["a", "new", "vid"]);
    }

    #[test]
    fn stepping_stops_at_either_end() {
        let ids = result_set(&[note("a", "text"), note("b", "text")]);
        assert!(viewing("a").stepped(&ids, Step::Previous).is_none());
        assert!(viewing("b").stepped(&ids, Step::Next).is_none());
        assert!(
            viewing("only")
                .stepped(&result_set(&[note("only", "text")]), Step::Next)
                .is_none()
        );
    }

    #[test]
    fn rapid_steps_each_count_from_the_last_one() {
        let ids = result_set(&[note("a", "text"), image("b"), note("c", "link"), image("d")]);
        // The grid reads the open item at each press, so presses faster
        // than it renders don't all start from the same item...
        assert_eq!(after(&ids, viewing("a"), &[Step::Next; 3]).id, "d");
        // ...and more of them than there are items just stop at the end.
        assert_eq!(after(&ids, viewing("a"), &[Step::Next; 10]).id, "d");
        let there_and_back = [
            Step::Next,
            Step::Next,
            Step::Previous,
            Step::Next,
            Step::Next,
        ];
        assert_eq!(after(&ids, viewing("a"), &there_and_back).id, "d");
    }

    #[test]
    fn stepping_uses_the_current_result_set() {
        let open = viewing("b");
        let before = result_set(&[note("a", "text"), note("b", "text"), note("c", "text")]);
        assert_eq!(open.stepped(&before, Step::Next).unwrap().id, "c");
        // "c" deleted meanwhile (or filtered out): the set has changed.
        let after_delete = result_set(&[note("a", "text"), note("b", "text")]);
        assert!(open.stepped(&after_delete, Step::Next).is_none());
        // The open item itself is gone: nowhere to step from.
        let without_it = result_set(&[note("a", "text"), note("c", "text")]);
        assert!(open.stepped(&without_it, Step::Next).is_none());
    }

    #[test]
    fn no_stepping_while_editing() {
        let ids = result_set(&[note("a", "text"), note("b", "text")]);
        let editing = OpenItem {
            mode: ViewMode::Editing,
            ..viewing("a")
        };
        assert!(editing.stepped(&ids, Step::Next).is_none());
    }

    #[test]
    fn neighbouring_images_are_fetched_ahead() {
        let items = [
            image("a"),
            note("b", "text"),
            image("c"),
            file("d", "video", true),
        ];
        assert_eq!(neighbour_images(&items, "a"), Vec::<String>::new());
        assert_eq!(
            neighbour_images(&items, "b"),
            ["https://files/a.png", "https://files/c.png"]
        );
        // A video's player fetches its own URL.
        assert_eq!(neighbour_images(&items, "c"), Vec::<String>::new());
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

    fn saved_at(id: &str, created_at: &str) -> ListedItem {
        ListedItem {
            created_at: created_at.to_string(),
            ..item(id, "text")
        }
    }

    fn section_ids(
        sections: &[(Option<DayHeading>, Vec<ListedItem>)],
    ) -> Vec<(Option<DayHeading>, Vec<&str>)> {
        sections
            .iter()
            .map(|(heading, items)| {
                (
                    *heading,
                    items.iter().map(|item| item.id.as_str()).collect(),
                )
            })
            .collect()
    }

    fn day(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    #[test]
    fn items_are_grouped_by_day_keeping_their_order() {
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let today = day(2026, 9, 28);
        let newest_first = vec![
            saved_at("a", "2026-09-28T10:00:00Z"),
            saved_at("b", "2026-09-28T08:00:00Z"),
            saved_at("c", "2026-09-27T20:00:00Z"),
            saved_at("d", "2026-09-26T09:00:00Z"),
            saved_at("e", "2026-09-26T07:00:00Z"),
        ];
        assert_eq!(
            section_ids(&day_sections(newest_first.clone(), &utc, today)),
            [
                (Some(DayHeading::Today), vec!["a", "b"]),
                (Some(DayHeading::Yesterday), vec!["c"]),
                (Some(DayHeading::Date(day(2026, 9, 26))), vec!["d", "e"]),
            ]
        );

        let oldest_first: Vec<_> = newest_first.into_iter().rev().collect();
        assert_eq!(
            section_ids(&day_sections(oldest_first, &utc, today)),
            [
                (Some(DayHeading::Date(day(2026, 9, 26))), vec!["e", "d"]),
                (Some(DayHeading::Yesterday), vec!["c"]),
                (Some(DayHeading::Today), vec!["b", "a"]),
            ]
        );
    }

    #[test]
    fn days_follow_the_viewers_time_zone() {
        let kyiv_summer = chrono::FixedOffset::east_opt(3 * 3600).unwrap();
        // 22:30 UTC on the 27th is already the 28th in Kyiv.
        let items = vec![
            saved_at("late", "2026-09-27T22:30:00Z"),
            saved_at("earlier", "2026-09-27T20:30:00Z"),
        ];
        assert_eq!(
            section_ids(&day_sections(items, &kyiv_summer, day(2026, 9, 28))),
            [
                (Some(DayHeading::Today), vec!["late"]),
                (Some(DayHeading::Yesterday), vec!["earlier"]),
            ]
        );
    }

    #[test]
    fn a_day_is_headed_once_and_undated_items_go_last() {
        let utc = chrono::FixedOffset::east_opt(0).unwrap();
        let items = vec![
            saved_at("a", "2026-09-20T10:00:00Z"),
            saved_at("broken", "not a date"),
            saved_at("b", "2026-09-19T10:00:00Z"),
            saved_at("c", "2026-09-20T09:00:00Z"),
        ];
        assert_eq!(
            section_ids(&day_sections(items, &utc, day(2026, 9, 28))),
            [
                (Some(DayHeading::Date(day(2026, 9, 20))), vec!["a", "c"]),
                (Some(DayHeading::Date(day(2026, 9, 19))), vec!["b"]),
                (None, vec!["broken"]),
            ]
        );
    }

    #[test]
    fn day_headings_name_today_and_yesterday_then_the_date() {
        let today = day(2026, 9, 28);
        assert_eq!(DayHeading::new(today, today), DayHeading::Today);
        assert_eq!(
            DayHeading::new(day(2026, 9, 27), today),
            DayHeading::Yesterday
        );
        // No "the day before yesterday".
        assert_eq!(
            DayHeading::new(day(2026, 9, 26), today),
            DayHeading::Date(day(2026, 9, 26))
        );
        in_language(Language::Ukrainian, || {
            assert_eq!(DayHeading::Today.label(), "Сьогодні");
            assert_eq!(DayHeading::Yesterday.label(), "Вчора");
            assert_eq!(
                DayHeading::Date(day(2026, 9, 26)).label(),
                "26 вересня 2026"
            );
        });
        in_language(Language::English, || {
            assert_eq!(DayHeading::Today.label(), "Today");
            assert_eq!(DayHeading::Yesterday.label(), "Yesterday");
            assert_eq!(
                DayHeading::Date(day(2026, 9, 26)).label(),
                "26 September 2026"
            );
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
