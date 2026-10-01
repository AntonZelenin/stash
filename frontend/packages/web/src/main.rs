use std::sync::Arc;

use api::ApiClient;
use dioxus::prelude::*;
use ui::{
    ActivityTracker, AuthSession, Platform, Route, TurnstileSiteKey, use_init_analytics,
    use_init_localization, use_init_preferences,
};

mod config;
use config::{API_BASE_URL, TURNSTILE_SITE_KEY, analytics_config};

mod language_store;
use language_store::{LocalStorageLanguageStore, browser_languages};

mod preference_store;
use preference_store::LocalStoragePreferenceStore;

mod token_store;
use token_store::LocalStorageTokenStore;

const FAVICON: Asset = asset!("/assets/favicon.png");
const MAIN_CSS: Asset = asset!("/assets/main.css");

fn main() {
    dioxus::launch(App);
}

#[component]
fn App() -> Element {
    use_init_localization(Arc::new(LocalStorageLanguageStore), browser_languages());
    use_init_preferences(Arc::new(LocalStoragePreferenceStore));
    use_context_provider(|| {
        AuthSession::new(
            ApiClient::new(API_BASE_URL),
            Arc::new(LocalStorageTokenStore),
        )
    });
    use_context_provider(|| TurnstileSiteKey(TURNSTILE_SITE_KEY));
    // After the preferences and the session it reads.
    use_init_analytics(analytics_config(), Platform::Web);

    rsx! {
        document::Title { "Stash" }
        document::Link { rel: "icon", r#type: "image/png", href: FAVICON }
        document::Link { rel: "stylesheet", href: MAIN_CSS }

        ActivityTracker { Router::<Route> {} }
    }
}
