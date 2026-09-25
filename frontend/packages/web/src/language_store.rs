use ui::LanguageStore;
use web_sys::window;

const STORAGE_KEY: &str = "stash.language";

/// Persists the user's chosen UI language in the browser's `localStorage`.
pub struct LocalStorageLanguageStore;

impl LocalStorageLanguageStore {
    fn storage() -> Option<web_sys::Storage> {
        window()?.local_storage().ok()?
    }
}

impl LanguageStore for LocalStorageLanguageStore {
    fn load(&self) -> Option<String> {
        Self::storage()?.get_item(STORAGE_KEY).ok()?
    }

    fn save(&self, code: &str) {
        if let Some(storage) = Self::storage() {
            let _ = storage.set_item(STORAGE_KEY, code);
        }
    }
}

/// The browser's preferred languages, most preferred first
/// (`navigator.languages`, or just `navigator.language` where that's all
/// there is).
pub fn browser_languages() -> Vec<String> {
    let Some(navigator) = window().map(|window| window.navigator()) else {
        return Vec::new();
    };
    let languages: Vec<String> = navigator
        .languages()
        .iter()
        .filter_map(|language| language.as_string())
        .collect();
    if languages.is_empty() {
        navigator.language().into_iter().collect()
    } else {
        languages
    }
}
