//! Collections: the picker for choosing which ones an item goes in (in the
//! capture box and the item editor) and their chips.

use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::filters::{FILTERS_CSS, TAG_LIST_LIMIT, TAG_SEARCH_DEBOUNCE};
use crate::i18n::api_error_message;
use crate::icons::{IconClose, IconFolder};
use crate::viewer_nav::keep_step_keys;

/// Collection chips and the picker; loaded by every component that shows
/// either, so none relies on another being on the page.
pub(crate) const COLLECTIONS_CSS: Asset = asset!("/assets/styling/collections.css");

/// Collapses whitespace like the server does, for comparing what's typed
/// with existing collection names.
fn clean_collection_name(raw: &str) -> String {
    raw.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Whether `names` has `name`, ignoring case, as the server compares them.
pub(crate) fn contains_name(names: &[String], name: &str) -> bool {
    let name = name.to_lowercase();
    names.iter().any(|n| n.to_lowercase() == name)
}

/// `names` with `name` added, or removed if it's already there (ignoring
/// case).
pub(crate) fn toggled(names: &[String], name: &str) -> Vec<String> {
    if contains_name(names, name) {
        let name = name.to_lowercase();
        names
            .iter()
            .filter(|n| n.to_lowercase() != name)
            .cloned()
            .collect()
    } else {
        let mut names = names.to_vec();
        names.push(name.to_string());
        names
    }
}

/// The picker's options while nothing is typed: `pinned` (what was checked
/// when it opened) first, so it stays in reach however far down the list
/// it is, then checked names the server didn't list (just created here),
/// then the rest of `found`. Checking one doesn't move it, so nothing
/// shifts under the pointer.
fn browse_order(pinned: &[String], selected: &[String], found: &[String]) -> Vec<String> {
    let mut options: Vec<String> = Vec::new();
    let unlisted = selected.iter().filter(|name| !contains_name(found, name));
    for name in pinned.iter().chain(unlisted).chain(found) {
        if !contains_name(&options, name) {
            options.push(name.clone());
        }
    }
    options
}

/// A collection's chip: folder icon and name, with a × calling `on_remove`
/// when given.
#[component]
pub(crate) fn CollectionChip(
    name: String,
    #[props(default)] disabled: bool,
    #[props(default)] on_remove: Option<EventHandler<()>>,
) -> Element {
    rsx! {
        span { class: "tag-chip collection-chip", title: "{name}",
            IconFolder {}
            span { class: "tag-chip-name", "{name}" }
            if let Some(on_remove) = on_remove {
                button {
                    class: "tag-chip-remove",
                    r#type: "button",
                    title: t!("collections-remove"),
                    aria_label: t!("collections-remove-named", name: name.as_str()),
                    disabled,
                    onclick: move |evt| {
                        // Don't also act on a control the chip sits in.
                        evt.stop_propagation();
                        on_remove.call(());
                    },
                    IconClose {}
                }
            }
        }
    }
}

/// Compact multi-select dropdown of the user's collections, for choosing
/// the ones an item goes in. Put it in a `collection-picker-anchor` next to
/// the button that opens it; it drops from that anchor's left edge, or its
/// right with `align_right`.
///
/// A search box on top, then the matching collections as checkboxes
/// (`selected`: names checked, compared case-insensitively; with nothing
/// typed, the ones checked when it opened first, see `browse_order`). Checking or unchecking one calls
/// `on_toggle` with its name and leaves the dropdown open, so several can
/// be picked. "Create …" (or Enter) offers the typed name when no
/// collection has it: it's created once the item is saved. Escape or a
/// click outside calls `on_close`.
#[component]
pub(crate) fn CollectionPicker(
    selected: Vec<String>,
    busy: bool,
    #[props(default)] align_right: bool,
    on_toggle: EventHandler<String>,
    on_close: EventHandler<()>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut query = use_signal(String::new);
    let pinned = use_hook({
        let selected = selected.clone();
        move || selected
    });

    // Re-runs as the query changes; the delay debounces typing (a newer
    // query drops the pending fetch).
    let matches = use_resource(move || {
        let session = session.clone();
        let query = clean_collection_name(&query());
        async move {
            if !query.is_empty() {
                Delay::new(TAG_SEARCH_DEBOUNCE).await;
            }
            session.list_collections(query, TAG_LIST_LIMIT).await
        }
    });

    let typed = clean_collection_name(&query());
    let (names, exact_exists) = match &*matches.read() {
        Some(Ok(found)) => (
            found.iter().map(|c| c.name.clone()).collect::<Vec<_>>(),
            found
                .iter()
                .any(|c| c.name.to_lowercase() == typed.to_lowercase()),
        ),
        _ => (Vec::new(), false),
    };
    // While searching, just the matches.
    let options: Vec<String> = if typed.is_empty() {
        browse_order(&pinned, &selected, &names)
    } else {
        names.clone()
    };
    let can_create = !typed.is_empty() && !exact_exists && !contains_name(&selected, &typed);
    // Cloned up front: `rsx!` doesn't evaluate in source order.
    let enter_names = names;
    let enter_typed = typed.clone();

    rsx! {
        // The checkbox options' styles are the Filters panel's.
        document::Link { rel: "stylesheet", href: FILTERS_CSS }
        document::Link { rel: "stylesheet", href: COLLECTIONS_CSS }

        div { class: "collection-picker-backdrop", onclick: move |_| on_close.call(()) }
        div {
            class: if align_right { "collection-picker collection-picker-right" } else { "collection-picker" },
            role: "dialog",
            aria_label: t!("collections-label"),
            onkeydown: move |evt| {
                if evt.key() == Key::Escape {
                    // Close just the picker, not a viewer it's in.
                    evt.stop_propagation();
                    on_close.call(());
                }
            },
            input {
                class: "collection-picker-search",
                r#type: "search",
                placeholder: t!("collections-search-placeholder"),
                maxlength: "50",
                disabled: busy,
                value: "{query}",
                oninput: move |evt| query.set(evt.value()),
                onmounted: move |evt| async move {
                    let _ = evt.set_focus(true).await;
                },
                onkeydown: {
                    let names = enter_names;
                    let typed = enter_typed;
                    move |evt: KeyboardEvent| {
                        // ← → move the caret, not a viewer it's in.
                        keep_step_keys(&evt);
                        if evt.key() == Key::Enter {
                            // Inside a form, Enter would submit it.
                            evt.prevent_default();
                            if typed.is_empty() {
                                return;
                            }
                            // An existing collection in its stored spelling,
                            // else the typed name.
                            let name = names
                                .iter()
                                .find(|name| name.to_lowercase() == typed.to_lowercase())
                                .cloned()
                                .unwrap_or_else(|| typed.clone());
                            on_toggle.call(name);
                            query.set(String::new());
                        }
                    }
                },
            }
            div { class: "collection-picker-options",
                for name in options.clone() {
                    label { class: "filter-option", key: "{name}",
                        input {
                            r#type: "checkbox",
                            checked: contains_name(&selected, &name),
                            disabled: busy,
                            onchange: {
                                let name = name.clone();
                                move |_| on_toggle.call(name.clone())
                            },
                        }
                        span { "{name}" }
                    }
                }
                if can_create {
                    button {
                        class: "collection-picker-create",
                        r#type: "button",
                        disabled: busy,
                        onclick: {
                            let typed = typed.clone();
                            move |_| {
                                on_toggle.call(typed.clone());
                                query.set(String::new());
                            }
                        },
                        {t!("collections-create", name: typed.as_str())}
                    }
                }
                match &*matches.read() {
                    Some(Err(err)) => rsx! {
                        p { class: "filter-empty", {t!("collections-load-failed", error: api_error_message(err))} }
                    },
                    None if options.is_empty() => rsx! {
                        p { class: "filter-empty", {t!("common-loading")} }
                    },
                    Some(Ok(_)) if options.is_empty() && !can_create => rsx! {
                        p { class: "filter-empty",
                            if typed.is_empty() { {t!("collections-none-yet")} } else { {t!("collections-no-matches")} }
                        }
                    },
                    _ => rsx! {},
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn toggling_adds_a_missing_name_and_removes_a_present_one_ignoring_case() {
        let chosen = names(&["Trips"]);

        assert_eq!(toggled(&chosen, "Books"), names(&["Trips", "Books"]));
        assert_eq!(toggled(&chosen, "trips"), Vec::<String>::new());
        assert!(contains_name(&chosen, "TRIPS"));
        assert!(!contains_name(&chosen, "Trip"));
    }

    #[test]
    fn browsing_lists_what_was_checked_on_opening_first_and_never_reorders_on_check() {
        let found = names(&["Books", "Recipes", "Trips"]);

        // Opened with Trips checked: it leads.
        let opened = browse_order(&names(&["Trips"]), &names(&["Trips"]), &found);
        assert_eq!(opened, names(&["Trips", "Books", "Recipes"]));

        // Checking Recipes then leaves it where it was.
        let checked = browse_order(&names(&["Trips"]), &names(&["Trips", "Recipes"]), &found);
        assert_eq!(checked, opened);

        // A name created here (not listed by the server yet) shows, once.
        let created = browse_order(&names(&["Trips"]), &names(&["trips", "Wishlist"]), &found);
        assert_eq!(created, names(&["Trips", "Wishlist", "Books", "Recipes"]));
    }

    #[test]
    fn typed_names_are_cleaned_like_the_server_does() {
        assert_eq!(clean_collection_name("  summer   trips "), "summer trips");
        assert_eq!(clean_collection_name("   "), "");
    }
}
