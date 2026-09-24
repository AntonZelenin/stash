use std::time::Duration;

use api::Tag;
use dioxus::prelude::*;
use futures_timer::Delay;

use crate::AuthSession;
use crate::icons::IconClose;

const FILTERS_CSS: Asset = asset!("/assets/styling/filters.css");
/// Tag chips, shared with the other tag UI (see tags.css).
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

/// How long typing in a tag search box must pause before tags are fetched.
pub(crate) const TAG_SEARCH_DEBOUNCE: Duration = Duration::from_millis(150);
/// Selected tags shown as chips in the closed Tags control before the rest
/// collapse into "+N".
const MAX_VISIBLE_TAG_CHIPS: usize = 2;
const TAG_FILTER_LIST_LIMIT: u32 = 50;

/// The item-type filter. Applied by the server, to listing and search alike.
#[derive(Clone, Copy, PartialEq)]
pub enum TypeFilter {
    All,
    Notes,
    Images,
    Links,
    Files,
}

impl TypeFilter {
    pub const OPTIONS: [TypeFilter; 5] = [
        TypeFilter::All,
        TypeFilter::Notes,
        TypeFilter::Images,
        TypeFilter::Links,
        TypeFilter::Files,
    ];

    fn label(self) -> &'static str {
        match self {
            TypeFilter::All => "All",
            TypeFilter::Notes => "Notes",
            TypeFilter::Images => "Images",
            TypeFilter::Links => "Links",
            TypeFilter::Files => "Files",
        }
    }

    /// What the closed control shows: the selected type, or "All types".
    fn button_label(self) -> &'static str {
        match self {
            TypeFilter::All => "All types",
            other => other.label(),
        }
    }

    /// The API's `type` value; None for no type filter. "Notes" is this
    /// UI's name for plain `text` items.
    pub fn api_value(self) -> Option<&'static str> {
        match self {
            TypeFilter::All => None,
            TypeFilter::Notes => Some("text"),
            TypeFilter::Images => Some("image"),
            TypeFilter::Links => Some("link"),
            TypeFilter::Files => Some("file"),
        }
    }
}

/// `[ Images ▾ ]`: single-choice dropdown for the item type.
#[component]
pub fn TypeDropdown(value: Signal<TypeFilter>) -> Element {
    let mut open = use_signal(|| false);
    let mut value = value;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        div { class: "filter-control type-filter",
            button {
                class: if value() == TypeFilter::All { "filter-button" } else { "filter-button filter-button-active" },
                r#type: "button",
                onclick: move |_| open.toggle(),
                span { class: "filter-button-label", "{value().button_label()}" }
                span { class: "filter-chevron", "▾" }
            }
            if open() {
                // Invisible full-screen layer: a click anywhere outside the
                // dropdown lands here and closes it.
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                div { class: "filter-panel type-filter-panel",
                    for option in TypeFilter::OPTIONS {
                        button {
                            class: if value() == option { "type-option type-option-selected" } else { "type-option" },
                            r#type: "button",
                            onclick: move |_| {
                                value.set(option);
                                open.set(false);
                            },
                            span { class: "type-option-check", if value() == option { "✓" } }
                            "{option.label()}"
                        }
                    }
                }
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
                if chosen.is_empty() {
                    span { class: "filter-button-label", "Tags" }
                } else {
                    span { class: "tag-filter-chips",
                        for tag in chosen.iter().take(MAX_VISIBLE_TAG_CHIPS).cloned() {
                            span { class: "tag-chip", key: "{tag.id}", title: "{tag.name}",
                                span { class: "tag-chip-name", "{tag.name}" }
                                button {
                                    class: "tag-chip-remove",
                                    r#type: "button",
                                    title: "Remove filter",
                                    aria_label: "Remove {tag.name} filter",
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
                span { class: "filter-chevron", "▾" }
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
            session.list_tags(query, TAG_FILTER_LIST_LIMIT).await
        }
    });

    let is_selected = move |tag: &Tag| selected.read().iter().any(|t| t.id == tag.id);

    rsx! {
        div { class: "filter-panel tag-filter-panel",
            input {
                class: "tag-filter-search",
                r#type: "search",
                placeholder: "Search tags...",
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
                            if query().trim().is_empty() { "No tags yet — add some on your items." } else { "No matching tags" }
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
                        p { class: "tag-filter-empty", "Could not load tags: {err}" }
                    },
                    None => rsx! {
                        p { class: "tag-filter-empty", "Loading..." }
                    },
                }
            }
        }
    }
}
