//! Stepping from an open item to the previous or next one of the result set
//! it was opened from (the list or search results, as filtered and sorted),
//! with ← → or the viewer's arrow buttons. It doesn't wrap around.

use dioxus::html::Modifiers;
use dioxus::prelude::*;
use dioxus_i18n::t;

/// A move through the result set.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Step {
    Previous,
    Next,
}

impl Step {
    pub(crate) fn label(self) -> String {
        match self {
            Self::Previous => t!("item-previous"),
            Self::Next => t!("item-next"),
        }
    }
}

/// The id `step` away from `current` in `ids` (the result set, in its shown
/// order); None at either end, or if `current` has left the set.
pub(crate) fn stepped<'a>(ids: &'a [String], current: &str, step: Step) -> Option<&'a String> {
    let index = ids.iter().position(|id| id == current)?;
    match step {
        Step::Previous => index.checked_sub(1).and_then(|index| ids.get(index)),
        Step::Next => ids.get(index + 1),
    }
}

/// Whether `key` is one of the arrow keys that step.
fn is_step_key(key: &Key) -> bool {
    matches!(key, Key::ArrowLeft | Key::ArrowRight)
}

/// The step a key press in the viewer asks for: ← previous, → next. None
/// while `editing` (← → move the caret, and leaving would drop the
/// changes), and with a modifier held: Alt+← is the browser's Back, Shift+→
/// extends a selection.
pub(crate) fn step_for_key(key: &Key, modifiers: Modifiers, editing: bool) -> Option<Step> {
    let held = Modifiers::ALT | Modifiers::CONTROL | Modifiers::META | Modifiers::SHIFT;
    if editing || modifiers.intersects(held) {
        return None;
    }
    match key {
        Key::ArrowLeft => Some(Step::Previous),
        Key::ArrowRight => Some(Step::Next),
        _ => None,
    }
}

/// For a text field or media player inside a viewer: keeps ← → for it
/// (moving the caret, seeking) rather than stepping to another item.
pub(crate) fn keep_step_keys(evt: &KeyboardEvent) {
    if is_step_key(&evt.key()) {
        evt.stop_propagation();
    }
}

/// An open item's place in its result set, for the viewer's previous/next
/// controls; `on_step` moves to the neighbouring item.
#[derive(Clone, PartialEq)]
pub(crate) struct ViewerNav {
    pub has_previous: bool,
    pub has_next: bool,
    /// How this item was reached, if by stepping: that button gets focus,
    /// so pressing it again keeps going.
    pub arrived_by: Option<Step>,
    /// Full-size images of the neighbouring items, fetched ahead so
    /// stepping to them shows them at once.
    pub preload: Vec<String>,
    pub on_step: Callback<Step>,
}

impl ViewerNav {
    /// Navigation for `current` in `ids`; None when there's nowhere to go
    /// (a single item) or it's no longer in the set.
    pub(crate) fn new(
        ids: &[String],
        current: &str,
        arrived_by: Option<Step>,
        preload: Vec<String>,
        on_step: Callback<Step>,
    ) -> Option<Self> {
        if ids.len() < 2 || !ids.iter().any(|id| id == current) {
            return None;
        }
        Some(Self {
            has_previous: stepped(ids, current, Step::Previous).is_some(),
            has_next: stepped(ids, current, Step::Next).is_some(),
            arrived_by,
            preload,
            on_step,
        })
    }

    pub(crate) fn allows(&self, step: Step) -> bool {
        match step {
            Step::Previous => self.has_previous,
            Step::Next => self.has_next,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::i18n::Language;
    use crate::i18n::tests::in_language;

    fn ids(ids: &[&str]) -> Vec<String> {
        ids.iter().map(|id| id.to_string()).collect()
    }

    #[test]
    fn arrow_keys_step_backwards_and_forwards() {
        let none = Modifiers::empty();
        assert_eq!(
            step_for_key(&Key::ArrowLeft, none, false),
            Some(Step::Previous)
        );
        assert_eq!(
            step_for_key(&Key::ArrowRight, none, false),
            Some(Step::Next)
        );
        for other in [
            Key::ArrowUp,
            Key::ArrowDown,
            Key::Escape,
            Key::Enter,
            Key::Character("l".into()),
        ] {
            assert_eq!(step_for_key(&other, none, false), None, "{other:?}");
        }
    }

    #[test]
    fn arrow_keys_are_left_alone_while_editing() {
        for key in [Key::ArrowLeft, Key::ArrowRight] {
            assert_eq!(step_for_key(&key, Modifiers::empty(), true), None);
        }
    }

    #[test]
    fn arrow_keys_with_modifiers_are_left_alone() {
        for modifier in [
            Modifiers::ALT,
            Modifiers::CONTROL,
            Modifiers::META,
            Modifiers::SHIFT,
        ] {
            for key in [Key::ArrowLeft, Key::ArrowRight] {
                assert_eq!(step_for_key(&key, modifier, false), None, "{modifier:?}");
            }
        }
        // Lock keys don't count as held.
        assert_eq!(
            step_for_key(&Key::ArrowRight, Modifiers::CAPS_LOCK, false),
            Some(Step::Next)
        );
    }

    #[test]
    fn text_fields_keep_only_the_step_keys() {
        assert!(is_step_key(&Key::ArrowLeft));
        assert!(is_step_key(&Key::ArrowRight));
        // Escape still reaches the viewer (the tag picker handles it itself).
        assert!(!is_step_key(&Key::Escape));
        assert!(!is_step_key(&Key::ArrowUp));
    }

    #[test]
    fn stepping_follows_the_result_sets_order_without_wrapping() {
        // As the server ordered them, not alphabetically.
        let set = ids(&["c", "a", "d", "b"]);
        assert_eq!(
            stepped(&set, "a", Step::Next).map(String::as_str),
            Some("d")
        );
        assert_eq!(
            stepped(&set, "a", Step::Previous).map(String::as_str),
            Some("c")
        );
        assert_eq!(stepped(&set, "c", Step::Previous), None);
        assert_eq!(stepped(&set, "b", Step::Next), None);
        // Gone from the set (deleted, or filtered out).
        assert_eq!(stepped(&set, "x", Step::Next), None);
    }

    /// Runs `f` in a Dioxus scope (callbacks need one) with an `on_step`
    /// callback; returns what it returned and the steps it received.
    fn with_on_step<T>(f: impl FnOnce(Callback<Step>) -> T) -> (T, Vec<Step>) {
        in_language(Language::English, || {
            let steps = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
            let on_step = Callback::new({
                let steps = steps.clone();
                move |step| steps.borrow_mut().push(step)
            });
            let result = f(on_step);
            (result, steps.take())
        })
    }

    #[test]
    fn controls_are_disabled_at_either_end() {
        let set = ids(&["a", "b", "c"]);
        with_on_step(|on_step| {
            let first = ViewerNav::new(&set, "a", None, Vec::new(), on_step).unwrap();
            assert!(!first.allows(Step::Previous) && first.allows(Step::Next));
            let middle = ViewerNav::new(&set, "b", None, Vec::new(), on_step).unwrap();
            assert!(middle.allows(Step::Previous) && middle.allows(Step::Next));
            let last = ViewerNav::new(&set, "c", None, Vec::new(), on_step).unwrap();
            assert!(last.allows(Step::Previous) && !last.allows(Step::Next));
        });
    }

    #[test]
    fn buttons_step_only_where_allowed() {
        let set = ids(&["a", "b"]);
        // What the viewer's buttons (and keys) do when pressed.
        let press = |nav: &ViewerNav, step| {
            if nav.allows(step) {
                nav.on_step.call(step);
            }
        };
        let ((), steps) = with_on_step(|on_step| {
            let first = ViewerNav::new(&set, "a", None, Vec::new(), on_step).unwrap();
            press(&first, Step::Previous);
            press(&first, Step::Next);
            let last = ViewerNav::new(&set, "b", Some(Step::Next), Vec::new(), on_step).unwrap();
            press(&last, Step::Next);
            press(&last, Step::Previous);
        });
        assert_eq!(steps, [Step::Next, Step::Previous]);
    }

    #[test]
    fn no_controls_for_a_single_item_or_one_that_left_the_set() {
        with_on_step(|on_step| {
            assert!(ViewerNav::new(&ids(&["a"]), "a", None, Vec::new(), on_step).is_none());
            assert!(ViewerNav::new(&ids(&["a", "b"]), "x", None, Vec::new(), on_step).is_none());
            assert!(ViewerNav::new(&[], "a", None, Vec::new(), on_step).is_none());
        });
    }

    #[test]
    fn controls_are_labelled_in_the_ui_language() {
        let labels =
            |language| in_language(language, || (Step::Previous.label(), Step::Next.label()));
        assert_eq!(
            labels(Language::English),
            ("Previous item".to_string(), "Next item".to_string())
        );
        assert_eq!(
            labels(Language::Ukrainian),
            (
                "Попередній елемент".to_string(),
                "Наступний елемент".to_string()
            )
        );
    }
}
