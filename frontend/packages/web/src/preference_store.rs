use ui::PreferenceStore;
use web_sys::window;

/// Prefixed to every preference's key, like the app's other entries.
const KEY_PREFIX: &str = "stash.";

/// Persists the user's viewing preferences in the browser's
/// `localStorage`.
pub struct LocalStoragePreferenceStore;

impl LocalStoragePreferenceStore {
    fn storage() -> Option<web_sys::Storage> {
        window()?.local_storage().ok()?
    }
}

impl PreferenceStore for LocalStoragePreferenceStore {
    fn load(&self, key: &str) -> Option<String> {
        Self::storage()?
            .get_item(&format!("{KEY_PREFIX}{key}"))
            .ok()?
    }

    fn save(&self, key: &str, value: &str) {
        if let Some(storage) = Self::storage() {
            let _ = storage.set_item(&format!("{KEY_PREFIX}{key}"), value);
        }
    }
}
