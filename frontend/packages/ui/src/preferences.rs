//! Viewing preferences: the hidden tags (chosen in Settings) and the
//! Normal/Blind mode (toggled in the top bar). In Blind mode, items
//! carrying any hidden tag are left out of the list, search, counts and
//! "Surprise me": the server does the excluding (`exclude_tag_id`), on top
//! of every other filter.
//!
//! The mode is this device's own. The hidden tags are kept with the
//! account (`GET /tags/hidden`, `POST /tags/visibility`), so every device
//! hides the same ones; this device keeps a copy, used from the moment the
//! app opens until the account's list arrives (`sync_hidden_tags`), so
//! Blind mode never shows hidden items in between. Hidden tags this device
//! kept on its own, before they were kept with the account, are uploaded
//! once.

use std::sync::Arc;

use dioxus::prelude::*;

use crate::AuthSession;

/// This device's copy of the account's hidden tags (before they were kept
/// with the account: the only list).
const HIDDEN_TAGS_KEY: &str = "hidden-tags";
/// Set once the hidden tags this device kept on its own were uploaded to
/// an account.
const HIDDEN_TAGS_IMPORTED_KEY: &str = "hidden-tags-imported";
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

    /// The hidden tags as the account now has them (from the server, or
    /// after a change was saved there), also kept as this device's copy.
    pub(crate) fn set_hidden_tag_ids(&self, ids: Vec<String>) {
        let ids = decode_ids(&encode_ids(&ids));
        self.store.save(HIDDEN_TAGS_KEY, &encode_ids(&ids));
        let mut slot = self.hidden_tag_ids;
        if *slot.peek() != ids {
            slot.set(ids);
        }
    }

    /// Hides the tag `id` for the account if it isn't hidden yet (unless
    /// `MAX_HIDDEN_TAGS` already are), otherwise shows it again; applied
    /// here once the server has saved it.
    pub(crate) async fn toggle_hidden_tag(
        &self,
        session: &AuthSession,
        id: &str,
    ) -> Result<(), api::ApiError> {
        let current = self.hidden_tag_ids.peek().clone();
        let hide = !current.iter().any(|existing| existing == id);
        if hide && current.len() >= MAX_HIDDEN_TAGS {
            return Ok(());
        }
        session
            .set_tag_visibility(vec![id.to_string()], hide, false)
            .await?;
        // Toggled against the list as it is now, which may have changed
        // while saving.
        let latest = self.hidden_tag_ids.peek().clone();
        let shown = latest.iter().any(|existing| existing == id);
        if shown != hide {
            self.set_hidden_tag_ids(toggled_id(&latest, id));
        }
        Ok(())
    }

    pub fn display_mode(&self) -> DisplayMode {
        (self.display_mode)()
    }

    /// The mode right now, without subscribing the caller to changes (for
    /// event handlers, analytics).
    pub fn current_display_mode(&self) -> DisplayMode {
        *self.display_mode.peek()
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

/// Loads the account's hidden tags into `preferences`, once per sign-in,
/// after uploading those this device kept on its own (before they were
/// kept with the account) if it hasn't yet. Uploaded as
/// `imported_from_device`, so it isn't counted as the user hiding them.
/// Ids that aren't the account's tags are ignored by the server. On
/// failure, this device's copy stays in use.
pub(crate) async fn sync_hidden_tags(session: &AuthSession, preferences: &Preferences) {
    if preferences.store.load(HIDDEN_TAGS_IMPORTED_KEY).is_none() {
        let local = preferences.hidden_tag_ids.peek().clone();
        let imported =
            local.is_empty() || session.set_tag_visibility(local, true, true).await.is_ok();
        if imported {
            preferences.store.save(HIDDEN_TAGS_IMPORTED_KEY, "1");
        }
    }
    if let Ok(ids) = session.hidden_tag_ids().await {
        preferences.set_hidden_tag_ids(ids);
    }
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
