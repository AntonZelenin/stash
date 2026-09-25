//! Placeholder data for UI that the backend doesn't serve yet. Everything
//! here is to be replaced by real API calls (via `AuthSession`) once the
//! matching endpoints exist.

/// Initials for the account avatar in the top bar.
/// TODO: replace with the signed-in user's profile (no `/users/me` yet).
pub fn user_initials() -> &'static str {
    "JD"
}
