//! Placeholder data for UI that the backend doesn't serve yet. Everything
//! here is to be replaced by real API calls (via `AuthSession`) once the
//! matching endpoints exist.

/// Item counts shown on the type and favorites filters.
/// TODO: replace with a counts endpoint.
#[derive(Clone, Copy, PartialEq)]
pub struct ItemCounts {
    pub all: u32,
    pub notes: u32,
    pub images: u32,
    pub links: u32,
    pub files: u32,
    pub favorites: u32,
}

pub fn item_counts() -> ItemCounts {
    ItemCounts {
        all: 84,
        notes: 28,
        images: 34,
        links: 15,
        files: 7,
        favorites: 12,
    }
}

/// Initials for the account avatar in the top bar.
/// TODO: replace with the signed-in user's profile (no `/users/me` yet).
pub fn user_initials() -> &'static str {
    "JD"
}
