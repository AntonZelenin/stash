use dioxus::prelude::*;

use ui::{ActivityTracker, AnalyticsConfig, Navbar, Platform, use_init_analytics};
use views::{Blog, Home};

mod views;

#[derive(Debug, Clone, Routable, PartialEq)]
#[rustfmt::skip]
enum Route {
    #[layout(DesktopNavbar)]
    #[route("/")]
    Home {},
    #[route("/blog/:id")]
    Blog { id: i32 },
}

const MAIN_CSS: Asset = asset!("/assets/main.css");

fn main() {
    dioxus::launch(App);
}

#[component]
fn App() -> Element {
    // Build cool things ✌️
    use_init_analytics(analytics_config(), Platform::Desktop);

    rsx! {
        // Global app resources
        document::Link { rel: "stylesheet", href: MAIN_CSS }

        ActivityTracker { Router::<Route> {} }
    }
}

/// Product analytics (PostHog), compiled in like the web app's: off unless
/// `STASH_ANALYTICS_ENABLED=true` and both `STASH_POSTHOG_PROJECT_API_KEY`
/// and `STASH_POSTHOG_HOST` were set at build time. A build with an invalid
/// key or host fails here, at startup, rather than sending nowhere.
fn analytics_config() -> Option<AnalyticsConfig> {
    AnalyticsConfig::from_settings(
        option_env!("STASH_ANALYTICS_ENABLED"),
        option_env!("STASH_POSTHOG_PROJECT_API_KEY"),
        option_env!("STASH_POSTHOG_HOST"),
    )
    .expect("invalid STASH_POSTHOG_PROJECT_API_KEY or STASH_POSTHOG_HOST")
}

/// A desktop-specific Router around the shared `Navbar` component
/// which allows us to use the desktop-specific `Route` enum.
#[component]
fn DesktopNavbar() -> Element {
    rsx! {
        Navbar {
            Link {
                to: Route::Home {},
                "Home"
            }
            Link {
                to: Route::Blog { id: 1 },
                "Blog"
            }
        }

        Outlet::<Route> {}
    }
}
