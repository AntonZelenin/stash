//! Short-lived messages at the bottom of the page, for a failure that has
//! nowhere else to show (e.g. a file that couldn't be opened).
//!
//! A page provides `Toasts` with `use_toasts` and renders a `ToastHost`;
//! anything below it shows a message with `show_toast`.

use std::time::Duration;

use dioxus::prelude::*;
use futures_timer::Delay;

const TOAST_CSS: Asset = asset!("/assets/styling/toast.css");

/// How long a message stays up.
const TOAST_DURATION: Duration = Duration::from_secs(5);

/// The message shown, if any, numbered so a newer one isn't hidden by an
/// older one's timer.
#[derive(Clone, Copy)]
pub(crate) struct Toasts(Signal<Option<(u64, String)>>);

/// Provides `Toasts` to the components below the caller.
pub(crate) fn use_toasts() -> Toasts {
    use_context_provider(|| Toasts(Signal::new(None)))
}

/// Shows `message`, replacing any shown; nothing if no page provides
/// `Toasts`.
pub(crate) fn show_toast(message: String) {
    if let Some(Toasts(mut toast)) = try_consume_context::<Toasts>() {
        let id = toast.peek().as_ref().map_or(0, |(id, _)| id + 1);
        toast.set(Some((id, message)));
    }
}

/// The message, while it's up.
#[component]
pub(crate) fn ToastHost() -> Element {
    let Toasts(mut toast) = use_context::<Toasts>();
    let shown = toast();
    let shown_id = shown.as_ref().map(|(id, _)| *id);
    use_effect(use_reactive!(|shown_id| {
        if let Some(id) = shown_id {
            spawn(async move {
                Delay::new(TOAST_DURATION).await;
                if toast.peek().as_ref().is_some_and(|(shown, _)| *shown == id) {
                    toast.set(None);
                }
            });
        }
    }));

    rsx! {
        document::Link { rel: "stylesheet", href: TOAST_CSS }
        div { class: "toast-region", role: "status", aria_live: "polite",
            if let Some((id, message)) = shown {
                div { key: "{id}", class: "toast", "{message}" }
            }
        }
    }
}
