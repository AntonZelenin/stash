use std::sync::Arc;

use api::ApiClient;
use dioxus::prelude::*;
use ui::{AuthSession, Route};

mod config;
use config::API_BASE_URL;

mod token_store;
use token_store::LocalStorageTokenStore;

const FAVICON: Asset = asset!("/assets/favicon.ico");
const MAIN_CSS: Asset = asset!("/assets/main.css");

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
        document::Title { "Stash" }
        document::Link { rel: "icon", href: FAVICON }
        document::Link { rel: "stylesheet", href: MAIN_CSS }

        Router::<Route> {}
    }
}
