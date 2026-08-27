use crate::models::TokenPair;

/// Persists the current token pair across app restarts/reloads. Storage is
/// inherently platform-specific (browser localStorage, a native keychain,
/// ...), so this crate only defines the contract; each platform crate that
/// needs auth provides its own implementation.
pub trait TokenStore {
    fn load(&self) -> Option<TokenPair>;
    fn save(&self, tokens: &TokenPair);
    fn clear(&self);
}
