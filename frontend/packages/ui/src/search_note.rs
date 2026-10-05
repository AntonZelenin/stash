//! An item's search note: extra words the user adds for search, apart from
//! the caption (or a note's text). Never shown on the item's card: only in
//! the capture box, while being written (collapsed until asked for), and
//! when the item is opened or edited.

use api::MAX_SEARCH_NOTE_LENGTH;
use dioxus::prelude::*;
use dioxus_i18n::t;

/// Whether the capture box shows its search note field: once the user
/// asked for it (`open`), and for as long as it holds text, so nothing
/// typed is ever hidden (e.g. when a send failed and kept it).
pub(crate) fn search_note_shown(open: bool, note: &str) -> bool {
    open || !note.is_empty()
}

/// `open` after "+ Add search note" is clicked: it opens the field, or
/// closes it again while it's still empty.
fn toggled_open(open: bool, note: &str) -> bool {
    !open || !note.is_empty()
}

/// "+ Add search note", beside "+ Add tag" in the capture box's actions
/// row: shows `SearchNoteInput` below the row (highlighted while it's
/// shown, like "+ Collection" with collections picked).
#[component]
pub(crate) fn AddSearchNoteButton(open: Signal<bool>, note: String, disabled: bool) -> Element {
    let shown = search_note_shown(open(), &note);
    rsx! {
        button {
            class: if shown { "home-option-add home-option-add-active" } else { "home-option-add" },
            r#type: "button",
            title: t!("search-note-add-title"),
            aria_expanded: if shown { "true" } else { "false" },
            disabled,
            onclick: move |_| open.set(toggled_open(open(), &note)),
            span { class: "home-option-plus", "+" }
            {t!("search-note-add")}
        }
    }
}

/// The capture box's search note field, across its whole width, under the
/// actions row: the same growing text area as the capture box's own (see
/// `Home`), so it reads as a second line of the same message. It takes
/// focus when it appears. Enter calls `on_submit`, as in the text area;
/// Shift+Enter is a new line.
#[component]
pub(crate) fn SearchNoteInput(
    value: Signal<String>,
    disabled: bool,
    on_submit: EventHandler<()>,
) -> Element {
    rsx! {
        div { class: "home-search-note",
            div { class: "home-input-grow",
                textarea {
                    class: "home-input",
                    rows: 1,
                    maxlength: "{MAX_SEARCH_NOTE_LENGTH}",
                    placeholder: t!("search-note-placeholder"),
                    aria_label: t!("search-note-label"),
                    value: "{value}",
                    disabled,
                    oninput: move |evt| value.set(evt.value()),
                    onmounted: move |evt| async move {
                        let _ = evt.set_focus(true).await;
                    },
                    // Not while an IME is composing, where Enter confirms
                    // the composition.
                    onkeydown: move |evt| {
                        if evt.key() == Key::Enter && !evt.modifiers().shift() && !evt.is_composing() {
                            evt.prevent_default();
                            on_submit.call(());
                        }
                    },
                }
                // Trailing space: a final newline would otherwise add no
                // height.
                div { class: "home-input-mirror", aria_hidden: "true", "{value} " }
            }
        }
    }
}

/// An opened item's search note, under its tags and quieter than its own
/// content. Without one, "+ Add search note" calls `on_add` (which opens
/// the edit form, where it's written).
#[component]
pub(crate) fn ItemSearchNote(note: Option<String>, on_add: EventHandler<()>) -> Element {
    match note {
        Some(note) => rsx! {
            div { class: "item-search-note",
                span { class: "item-search-note-label", {t!("search-note-label")} }
                p { class: "item-search-note-text", "{note}" }
            }
        },
        None => rsx! {
            button {
                class: "tag-add item-search-note-add",
                r#type: "button",
                title: t!("search-note-add-title"),
                onclick: move |_| on_add.call(()),
                "+ "
                {t!("search-note-add")}
            }
        },
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::i18n::{Language, LanguageStore, use_init_localization};

    struct English;

    impl LanguageStore for English {
        fn load(&self) -> Option<String> {
            Some(Language::English.code().into())
        }

        fn save(&self, _: &str) {}
    }

    /// `app` rendered to HTML once, in English.
    pub(crate) fn render(app: fn() -> Element) -> String {
        #[derive(Clone, Props)]
        struct RootProps {
            app: fn() -> Element,
        }

        // Rendered once, never with new props.
        impl PartialEq for RootProps {
            fn eq(&self, _: &Self) -> bool {
                false
            }
        }

        #[allow(non_snake_case)]
        fn Root(props: RootProps) -> Element {
            use_init_localization(Arc::new(English), Vec::new());
            (props.app)()
        }

        let mut dom = VirtualDom::new_with_props(Root, RootProps { app });
        dom.rebuild_in_place();
        dioxus_ssr::render(&dom)
    }

    /// The capture box's search note controls, as `Home` lays them out,
    /// with the field `open` and holding `note`.
    fn capture_box(open: bool, note: &str) -> String {
        thread_local! {
            static STATE: std::cell::RefCell<(bool, String)> = Default::default();
        }
        STATE.with(|state| *state.borrow_mut() = (open, note.to_string()));

        #[allow(non_snake_case)]
        fn CaptureBox() -> Element {
            let (open, note) = STATE.with(|state| state.borrow().clone());
            let open = use_signal(|| open);
            let note = use_signal(|| note);
            rsx! {
                AddSearchNoteButton { open, note: note(), disabled: false }
                if search_note_shown(open(), &note()) {
                    SearchNoteInput { value: note, disabled: false, on_submit: |_| {} }
                }
            }
        }
        render(CaptureBox)
    }

    #[test]
    fn the_capture_box_starts_with_the_field_collapsed() {
        let html = capture_box(false, "");

        assert!(html.contains("Add search note"), "{html}");
        assert!(html.contains(r#"aria-expanded="false""#), "{html}");
        assert!(!html.contains("home-search-note"), "{html}");
    }

    #[test]
    fn add_search_note_expands_the_field() {
        assert!(toggled_open(false, ""));

        let html = capture_box(true, "");
        assert!(html.contains("home-search-note"), "{html}");
        assert!(html.contains(r#"aria-expanded="true""#), "{html}");
        assert!(html.contains("Extra words to find it by"), "{html}");
    }

    #[test]
    fn clicking_again_collapses_an_empty_field() {
        assert!(!toggled_open(true, ""));
    }

    #[test]
    fn a_typed_note_stays_shown_and_kept() {
        // Clicking again doesn't hide what was typed...
        assert!(toggled_open(true, "granny's recipe"));
        // ...and text kept after a rerender (e.g. a failed send) shows the
        // field with it, even if it wasn't opened again.
        let html = capture_box(false, "granny's recipe");
        assert!(html.contains("home-search-note"), "{html}");
        assert!(
            html.contains("granny&#39;s recipe") || html.contains("granny's recipe"),
            "{html}"
        );
    }

    #[test]
    fn an_opened_item_shows_its_note() {
        #[allow(non_snake_case)]
        fn WithNote() -> Element {
            rsx! {
                ItemSearchNote { note: Some("tax return 2025".to_string()), on_add: |_| {} }
            }
        }
        let html = render(WithNote);

        assert!(html.contains("Search note"), "{html}");
        assert!(html.contains("tax return 2025"), "{html}");
        assert!(!html.contains("Add search note"), "{html}");
    }

    #[test]
    fn an_opened_item_without_a_note_offers_to_add_one() {
        #[allow(non_snake_case)]
        fn WithoutNote() -> Element {
            rsx! {
                ItemSearchNote { note: None, on_add: |_| {} }
            }
        }
        let html = render(WithoutNote);

        assert!(html.contains("+ Add search note"), "{html}");
        assert!(!html.contains("item-search-note-text"), "{html}");
    }
}
