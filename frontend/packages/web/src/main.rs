use std::sync::Arc;

use api::ApiClient;
use dioxus::prelude::*;
use ui::{AuthSession, Route};

mod token_store;
use token_store::LocalStorageTokenStore;

const FAVICON: Asset = asset!("/assets/favicon.ico");
const MAIN_CSS: Asset = asset!("/assets/main.css");

/// The backend API's base URL. Assumes the local dev/docker-compose setup
/// (`docker-compose.yml`'s `API_PORT`, default 8000).
const API_BASE_URL: &str = "http://localhost:8000";

fn main() {
    dioxus::launch(App);
}

#[component]
fn App() -> Element {
    use_context_provider(|| {
        AuthSession::new(
            ApiClient::new(API_BASE_URL),
            Arc::new(LocalStorageTokenStore),
        )
    });

    rsx! {
        document::Link { rel: "icon", href: FAVICON }
        document::Link { rel: "stylesheet", href: MAIN_CSS }

        Router::<Route> {}
    }
}
