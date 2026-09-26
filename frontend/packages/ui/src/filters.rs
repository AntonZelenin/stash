use std::time::Duration;

use api::{ItemCounts, ItemSort, Tag};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::i18n::api_error_message;
use crate::icons::{
    IconCheck, IconChevronDown, IconClose, IconHeart, IconHeartFilled, IconSort, IconTag,
};

const FILTERS_CSS: Asset = asset!("/assets/styling/filters.css");
/// Tag chips, shared with the other tag UI (see tags.css).
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

/// How long typing in a tag search box must pause before tags are fetched.
pub(crate) const TAG_SEARCH_DEBOUNCE: Duration = Duration::from_millis(150);
/// Selected tags shown as chips in the closed Tags control before the rest
/// collapse into "+N".
const MAX_VISIBLE_TAG_CHIPS: usize = 2;
/// How many of the user's tags a tag list asks for: the Tags filter's and
/// the one under an item's "Add tag" input. Both lists scroll.
pub(crate) const TAG_LIST_LIMIT: u32 = 50;

/// The item-type filter. Applied by the server, to listing and search alike.
#[derive(Clone, Copy, PartialEq)]
pub enum TypeFilter {
    All,
    Text,
    Images,
    Links,
    Files,
}

impl TypeFilter {
    pub const OPTIONS: [TypeFilter; 5] = [
        TypeFilter::All,
        TypeFilter::Text,
        TypeFilter::Images,
        TypeFilter::Links,
        TypeFilter::Files,
    ];

    fn label(self) -> String {
        match self {
            TypeFilter::All => t!("nav-all-items"),
            TypeFilter::Text => t!("nav-text-notes"),
            TypeFilter::Images => t!("nav-images"),
            TypeFilter::Links => t!("nav-links"),
            TypeFilter::Files => t!("nav-files"),
        }
    }

    /// CSS modifier giving each type its accent color (dot, hover tint).
    fn color_class(self) -> &'static str {
        match self {
            TypeFilter::All => "type-tab-all",
            TypeFilter::Text => "type-tab-notes",
            TypeFilter::Images => "type-tab-images",
            TypeFilter::Links => "type-tab-links",
            TypeFilter::Files => "type-tab-files",
        }
    }

    fn count(self, counts: &ItemCounts) -> u32 {
        let types = &counts.types;
        match self {
            TypeFilter::All => types.total(),
            TypeFilter::Text => types.text,
            TypeFilter::Images => types.image,
            TypeFilter::Links => types.link,
            TypeFilter::Files => types.file,
        }
    }

    /// The API's `type` value; None for no type filter. "Notes" is this
    /// UI's name for plain `text` items.
    pub fn api_value(self) -> Option<&'static str> {
        match self {
            TypeFilter::All => None,
            TypeFilter::Text => Some("text"),
            TypeFilter::Images => Some("image"),
            TypeFilter::Links => Some("link"),
            TypeFilter::Files => Some("file"),
        }
    }
}

/// `[ All Stashes 84 ] • Notes 28  • Images & Media 34 …`: one segment per
/// item type, each with its count (left out until `counts` has loaded); the
/// selected one is filled.
#[component]
pub fn TypeTabs(value: Signal<TypeFilter>, counts: Option<ItemCounts>) -> Element {
    let mut value = value;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        for option in TypeFilter::OPTIONS {
            button {
                class: if value() == option { "type-tab {option.color_class()} type-tab-selected" } else { "type-tab {option.color_class()}" },
                r#type: "button",
                aria_pressed: if value() == option { "true" } else { "false" },
                onclick: move |_| value.set(option),
                if option != TypeFilter::All {
                    span { class: "type-tab-dot" }
                }
                span { "{option.label()}" }
                if let Some(counts) = &counts {
                    span { class: "filter-count", "{option.count(counts)}" }
                }
            }
        }
    }
}

/// `[ ♥ Favorites 12 ]`: on/off toggle for showing only favorites. The
/// count is left out until it has loaded.
#[component]
pub fn FavoritesToggle(value: Signal<bool>, count: Option<u32>) -> Element {
    let mut value = value;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        button {
            class: if value() { "favorites-toggle favorites-toggle-active" } else { "favorites-toggle" },
            r#type: "button",
            aria_pressed: if value() { "true" } else { "false" },
            title: if value() { t!("nav-favorites-only-on") } else { t!("nav-favorites-only-off") },
            onclick: move |_| value.toggle(),
            if value() {
                IconHeartFilled {}
            } else {
                IconHeart {}
            }
            span { {t!("nav-favorites")} }
            if let Some(count) = count {
                span { class: "favorites-count", "{count}" }
            }
        }
    }
}

/// `[ Python ×  Architecture ×  +2 ▾ ]`: searchable multi-select of the
/// user's tags. The closed control shows the selection as removable chips
/// (the first few, then "+N"); the open panel has the search box and a
/// checkbox per matching tag.
#[component]
pub fn TagFilter(selected: Signal<Vec<Tag>>) -> Element {
    let mut open = use_signal(|| false);
    let mut selected = selected;

    let chosen = selected();
    let hidden = chosen.len().saturating_sub(MAX_VISIBLE_TAG_CHIPS);

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }

        div { class: "filter-control tag-filter",
            // A div rather than a button: it contains the chips' own ×
            // buttons, and buttons can't nest.
            div {
                class: if chosen.is_empty() { "filter-button" } else { "filter-button filter-button-active" },
                role: "button",
                tabindex: "0",
                onclick: move |_| open.toggle(),
                onkeydown: move |evt| {
                    if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                        evt.prevent_default();
                        open.toggle();
                    }
                },
                IconTag {}
                if chosen.is_empty() {
                    span { class: "filter-button-label", {t!("tags-label")} }
                } else {
                    span { class: "tag-filter-chips",
                        for tag in chosen.iter().take(MAX_VISIBLE_TAG_CHIPS).cloned() {
                            span { class: "tag-chip", key: "{tag.id}", title: "{tag.name}",
                                span { class: "tag-chip-name", "{tag.name}" }
                                button {
                                    class: "tag-chip-remove",
                                    r#type: "button",
                                    title: t!("tags-remove-filter"),
                                    aria_label: t!("tags-remove-filter-named", name: tag.name.as_str()),
                                    onclick: move |evt| {
                                        // Don't also toggle the dropdown.
                                        evt.stop_propagation();
                                        selected.write().retain(|t| t.id != tag.id);
                                    },
                                    IconClose {}
                                }
                            }
                        }
                        if hidden > 0 {
                            span { class: "tag-filter-more", "+{hidden}" }
                        }
                    }
                }
                span { class: "filter-chevron", IconChevronDown {} }
            }
            if open() {
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                TagFilterPanel { selected }
            }
        }
    }
}

/// The open Tags dropdown: search box on top, matching tags as checkboxes.
/// Mounted only while open, so it fetches the current tag list each time.
#[component]
fn TagFilterPanel(selected: Signal<Vec<Tag>>) -> Element {
    let session = use_context::<AuthSession>();
    let mut query = use_signal(String::new);
    let mut selected = selected;

    // Re-runs as the query changes; the delay debounces typing (a newer
    // query drops the pending fetch).
    let matches = use_resource(move || {
        let session = session.clone();
        let query = query().trim().to_string();
        async move {
            if !query.is_empty() {
                Delay::new(TAG_SEARCH_DEBOUNCE).await;
            }
            session.list_tags(query, TAG_LIST_LIMIT).await
        }
    });

    let is_selected = move |tag: &Tag| selected.read().iter().any(|t| t.id == tag.id);

    rsx! {
        div { class: "filter-panel tag-filter-panel",
            input {
                class: "tag-filter-search",
                r#type: "search",
                placeholder: t!("tags-search-placeholder"),
                value: "{query}",
                oninput: move |evt| query.set(evt.value()),
                onmounted: move |evt| async move {
                    let _ = evt.set_focus(true).await;
                },
            }
            div { class: "tag-filter-options",
                match &*matches.read() {
                    Some(Ok(tags)) if tags.is_empty() => rsx! {
                        p { class: "tag-filter-empty",
                            if query().trim().is_empty() { {t!("tags-none-yet")} } else { {t!("tags-no-matches")} }
                        }
                    },
                    Some(Ok(tags)) => rsx! {
                        for tag in tags.clone() {
                            label { class: "tag-filter-option", key: "{tag.id}",
                                input {
                                    r#type: "checkbox",
                                    checked: is_selected(&tag),
                                    onchange: move |_| {
                                        let already = selected.read().iter().any(|t| t.id == tag.id);
                                        if already {
                                            selected.write().retain(|t| t.id != tag.id);
                                        } else {
                                            selected.write().push(tag.clone());
                                        }
                                    },
                                }
                                span { "{tag.name}" }
                            }
                        }
                    },
                    Some(Err(err)) => rsx! {
                        p { class: "tag-filter-empty", {t!("tags-load-failed", error: api_error_message(err))} }
                    },
                    None => rsx! {
                        p { class: "tag-filter-empty", {t!("common-loading")} }
                    },
                }
            }
        }
    }
}

/// The sort orders offered, in menu order.
const SORT_OPTIONS: [ItemSort; 3] = [ItemSort::Newest, ItemSort::Oldest, ItemSort::Random];

fn sort_label(sort: ItemSort) -> String {
    match sort {
        ItemSort::Newest => t!("sort-newest"),
        ItemSort::Oldest => t!("sort-oldest"),
        ItemSort::Random => t!("sort-random"),
    }
}

/// `[ ⇅ ]`: listing order, at the right end of the filter bar. Highlighted
/// when not the default (newest first). Picking Random again reshuffles
/// (`on_reshuffle`). Disabled while searching, since search results are
/// ranked by relevance.
#[component]
pub fn SortMenu(
    value: Signal<ItemSort>,
    on_reshuffle: EventHandler<()>,
    disabled: bool,
) -> Element {
    let mut open = use_signal(|| false);
    let mut value = value;

    let current = value();
    let title = if disabled {
        t!("sort-search-relevance")
    } else {
        t!("sort-title", order: sort_label(current))
    };
    let mut class = String::from("filter-button sort-button");
    if current != ItemSort::default() {
        class.push_str(" filter-button-active");
    }

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        div { class: "filter-control sort-menu",
            button {
                class,
                r#type: "button",
                title: "{title}",
                aria_label: "{title}",
                aria_haspopup: "menu",
                aria_expanded: if open() { "true" } else { "false" },
                disabled,
                onclick: move |_| open.toggle(),
                IconSort {}
            }
            if open() && !disabled {
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                div { class: "filter-panel sort-panel", role: "menu",
                    for option in SORT_OPTIONS {
                        button {
                            class: if option == current { "sort-option sort-option-selected" } else { "sort-option" },
                            r#type: "button",
                            role: "menuitemradio",
                            aria_checked: if option == current { "true" } else { "false" },
                            title: if option == ItemSort::Random && current == ItemSort::Random { t!("sort-reshuffle") } else { String::new() },
                            onclick: move |_| {
                                if option == ItemSort::Random && value() == ItemSort::Random {
                                    on_reshuffle.call(());
                                } else {
                                    value.set(option);
                                }
                                open.set(false);
                            },
                            span { class: "sort-option-check",
                                if option == current {
                                    IconCheck {}
                                }
                            }
                            span { "{sort_label(option)}" }
                        }
                    }
                }
            }
        }
    }
}
