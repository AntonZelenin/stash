use std::sync::Arc;

use api::ApiClient;
use dioxus::prelude::*;
use ui::{AuthSession, Route, TurnstileSiteKey, use_init_localization};

mod config;
use config::{API_BASE_URL, TURNSTILE_SITE_KEY};

mod language_store;
use language_store::{LocalStorageLanguageStore, browser_languages};

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
    use_context_provider(|| {
        AuthSession::new(
            ApiClient::new(API_BASE_URL),
            Arc::new(LocalStorageTokenStore),
        )
    });
    use_context_provider(|| TurnstileSiteKey(TURNSTILE_SITE_KEY));

    rsx! {
        document::Title { "Stash" }
        document::Link { rel: "icon", r#type: "image/png", href: FAVICON }
        document::Link { rel: "stylesheet", href: MAIN_CSS }

        Router::<Route> {}
    }
}
