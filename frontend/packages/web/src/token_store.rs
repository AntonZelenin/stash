use api::{TokenPair, TokenStore};
use gloo_storage::{LocalStorage, Storage};

const STORAGE_KEY: &str = "stash.tokens";

/// Persists the current token pair in the browser's `localStorage`.
pub struct LocalStorageTokenStore;

impl TokenStore for LocalStorageTokenStore {
    fn load(&self) -> Option<TokenPair> {
        LocalStorage::get(STORAGE_KEY).ok()
    }

    fn save(&self, tokens: &TokenPair) {
        let _ = LocalStorage::set(STORAGE_KEY, tokens);
    }

    fn clear(&self) {
        LocalStorage::delete(STORAGE_KEY);
    }
}
