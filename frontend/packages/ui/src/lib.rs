//! This crate contains all shared UI for the workspace.

mod hero;
pub use hero::Hero;

mod navbar;
pub use navbar::Navbar;

mod icons;

mod i18n;
pub use i18n::{Language, LanguageStore, Localization, use_init_localization};

mod preferences;
pub use preferences::{PreferenceStore, Preferences, use_init_preferences};

mod auth;
pub use auth::Auth;

mod auth_session;
pub use auth_session::AuthSession;

mod turnstile;
pub use turnstile::TurnstileSiteKey;

mod collections;
mod confirm;
mod date_filter;
mod duplicates;
mod filters;
mod items;
mod media;
mod selection;
mod text_kind;
mod toast;
mod url_refresh;
mod viewer_nav;

mod settings;

mod home;
pub use home::Home;

mod routes;
pub use routes::Route;
