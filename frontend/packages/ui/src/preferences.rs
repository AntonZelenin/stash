//! Viewing preferences kept on this device between sessions: the hidden
//! tags (chosen in Settings) and the Normal/Blind mode (toggled in the top
//! bar). In Blind mode, items carrying any hidden tag are left out of the
//! list, search, counts and "Surprise me": the server does the excluding
//! (`exclude_tag_id`), on top of every other filter.

use std::sync::Arc;

use dioxus::prelude::*;

const HIDDEN_TAGS_KEY: &str = "hidden-tags";
const DISPLAY_MODE_KEY: &str = "display-mode";

/// Most tags that can be hidden: as many as one request can exclude (the
/// API's `MAX_EXCLUDED_TAGS`).
pub(crate) const MAX_HIDDEN_TAGS: usize = 100;

/// Where preferences are kept between sessions, by key. Implemented by each
/// platform (e.g. `localStorage` on the web).
pub trait PreferenceStore {
    fn load(&self, key: &str) -> Option<String>;
    fn save(&self, key: &str, value: &str);
}

/// Whether hidden tags take effect.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum DisplayMode {
    /// Everything is shown; hidden tags have no effect.
    #[default]
    Normal,
    /// Items carrying any hidden tag are left out.
    Blind,
}

impl DisplayMode {
    pub(crate) const ALL: [DisplayMode; 2] = [DisplayMode::Normal, DisplayMode::Blind];

    fn code(self) -> &'static str {
        match self {
            DisplayMode::Normal => "normal",
            DisplayMode::Blind => "blind",
        }
    }

    /// The mode saved as `code`; Normal for anything else (nothing saved
    /// yet, or an unknown value).
    fn from_code(code: Option<&str>) -> DisplayMode {
        DisplayMode::ALL
            .into_iter()
            .find(|mode| Some(mode.code()) == code)
            .unwrap_or_default()
    }
}

/// Shared, reactive preferences, provided by `use_init_preferences` and
/// read with `use_context::<Preferences>()`. Reading them in a component,
/// memo or resource subscribes it to changes.
#[derive(Clone)]
pub struct Preferences {
    store: Arc<dyn PreferenceStore>,
    hidden_tag_ids: Signal<Vec<String>>,
    display_mode: Signal<DisplayMode>,
}

impl PartialEq for Preferences {
    fn eq(&self, other: &Self) -> bool {
        self.hidden_tag_ids == other.hidden_tag_ids && self.display_mode == other.display_mode
    }
}

impl Preferences {
    /// The ids of the hidden tags, in the order they were hidden.
    pub fn hidden_tag_ids(&self) -> Vec<String> {
        (self.hidden_tag_ids)()
    }

    /// Hides the tag `id` if it isn't hidden yet (unless `MAX_HIDDEN_TAGS`
    /// already are), otherwise shows it again.
    pub fn toggle_hidden_tag(&self, id: &str) {
        let mut ids = self.hidden_tag_ids;
        let current = ids();
        let updated = toggled_id(&current, id);
        if updated.len() > MAX_HIDDEN_TAGS {
            return;
        }
        self.store.save(HIDDEN_TAGS_KEY, &encode_ids(&updated));
        ids.set(updated);
    }

    pub fn display_mode(&self) -> DisplayMode {
        (self.display_mode)()
    }

    pub fn set_display_mode(&self, mode: DisplayMode) {
        let mut slot = self.display_mode;
        slot.set(mode);
        self.store.save(DISPLAY_MODE_KEY, mode.code());
    }

    /// The tag ids to leave out of what's shown: the hidden ones in Blind
    /// mode, none in Normal mode.
    pub fn excluded_tag_ids(&self) -> Vec<String> {
        match self.display_mode() {
            DisplayMode::Normal => Vec::new(),
            DisplayMode::Blind => self.hidden_tag_ids(),
        }
    }
}

/// Loads the saved preferences and provides them to the component tree
/// below the caller. Call once, in the app root.
pub fn use_init_preferences(store: Arc<dyn PreferenceStore>) -> Preferences {
    use_context_provider(|| {
        let hidden_tag_ids = decode_ids(store.load(HIDDEN_TAGS_KEY).as_deref().unwrap_or(""));
        let display_mode = DisplayMode::from_code(store.load(DISPLAY_MODE_KEY).as_deref());
        Preferences {
            store,
            hidden_tag_ids: Signal::new(hidden_tag_ids),
            display_mode: Signal::new(display_mode),
        }
    })
}

/// `ids` with `id` removed if it's there, else added at the end.
fn toggled_id(ids: &[String], id: &str) -> Vec<String> {
    if ids.iter().any(|existing| existing == id) {
        ids.iter()
            .filter(|existing| *existing != id)
            .cloned()
            .collect()
    } else {
        ids.iter().cloned().chain([id.to_string()]).collect()
    }
}

/// Tag ids are UUIDs, so a comma can't be part of one.
fn encode_ids(ids: &[String]) -> String {
    ids.join(",")
}

/// The saved ids, without blanks or repeats, at most `MAX_HIDDEN_TAGS`.
fn decode_ids(saved: &str) -> Vec<String> {
    let mut ids: Vec<String> = Vec::new();
    for id in saved.split(',').map(str::trim).filter(|id| !id.is_empty()) {
        if ids.len() < MAX_HIDDEN_TAGS && !ids.iter().any(|existing| existing == id) {
            ids.push(id.to_string());
        }
    }
    ids
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toggling_adds_then_removes_an_id() {
        let ids = toggled_id(&[], "a");
        assert_eq!(ids, ["a"]);
        let ids = toggled_id(&ids, "b");
        assert_eq!(ids, ["a", "b"]);
        assert_eq!(toggled_id(&ids, "a"), ["b"]);
    }

    #[test]
    fn ids_round_trip_through_storage() {
        let ids = vec!["1f0c".to_string(), "9a2b".to_string()];
        assert_eq!(decode_ids(&encode_ids(&ids)), ids);
        assert_eq!(decode_ids(&encode_ids(&[])), Vec::<String>::new());
    }

    #[test]
    fn saved_ids_are_cleaned_up() {
        assert_eq!(decode_ids(" a,,b ,a,"), ["a", "b"]);
        let many: Vec<String> = (0..MAX_HIDDEN_TAGS + 5).map(|i| i.to_string()).collect();
        assert_eq!(decode_ids(&encode_ids(&many)).len(), MAX_HIDDEN_TAGS);
    }

    #[test]
    fn display_mode_defaults_to_normal() {
        assert_eq!(DisplayMode::from_code(None), DisplayMode::Normal);
        assert_eq!(DisplayMode::from_code(Some("other")), DisplayMode::Normal);
        for mode in DisplayMode::ALL {
            assert_eq!(DisplayMode::from_code(Some(mode.code())), mode);
        }
    }
}
