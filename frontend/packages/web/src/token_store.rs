use api::{TokenPair, TokenStore};
use web_sys::window;

const STORAGE_KEY: &str = "stash.tokens";

/// Persists the current token pair in the browser's `localStorage`.
pub struct LocalStorageTokenStore;

impl LocalStorageTokenStore {
    fn storage() -> Option<web_sys::Storage> {
        window()?.local_storage().ok()?
    }
}

impl TokenStore for LocalStorageTokenStore {
    fn load(&self) -> Option<TokenPair> {
        let raw = Self::storage()?.get_item(STORAGE_KEY).ok()??;
        serde_json::from_str(&raw).ok()
    }

    fn save(&self, tokens: &TokenPair) {
        let Some(storage) = Self::storage() else {
            return;
        };
        if let Ok(raw) = serde_json::to_string(tokens) {
            let _ = storage.set_item(STORAGE_KEY, &raw);
        }
    }

    fn clear(&self) {
        if let Some(storage) = Self::storage() {
            let _ = storage.remove_item(STORAGE_KEY);
        }
    }
}
