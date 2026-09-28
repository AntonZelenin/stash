//! Asking the user to confirm a destructive action.

use dioxus::prelude::*;
use dioxus_i18n::t;

const CONFIRM_CSS: Asset = asset!("/assets/styling/confirm.css");

/// A small modal over a dimmed backdrop, like the settings window, above
/// everything else (an open item's view included): `title`, `message`, an
/// `error` from a failed attempt if any, and Cancel / `confirm_label`
/// buttons. Cancel has focus at first, so Enter alone never confirms.
///
/// Only the confirm button calls `on_confirm`; Cancel, Escape and a click
/// outside the window call `on_cancel`. While `busy` (the action running),
/// both buttons are disabled, the confirm button reads `busy_label`, and
/// nothing cancels.
#[component]
pub(crate) fn ConfirmDialog(
    title: String,
    message: String,
    confirm_label: String,
    busy_label: String,
    busy: bool,
    #[props(default)] error: Option<String>,
    on_confirm: EventHandler<()>,
    on_cancel: EventHandler<()>,
) -> Element {
    // Whether the current click started on the backdrop itself (see
    // `AccountSettings`).
    let mut pressed_backdrop = use_signal(|| false);
    let cancel = move || {
        if !busy {
            on_cancel.call(());
        }
    };

    rsx! {
        document::Link { rel: "stylesheet", href: CONFIRM_CSS }

        div {
            class: "confirm-overlay",
            role: "alertdialog",
            aria_modal: "true",
            aria_label: "{title}",
            tabindex: "-1",
            onkeydown: move |evt| {
                if evt.key() == Key::Escape {
                    // Not also closing an item view behind it.
                    evt.stop_propagation();
                    cancel();
                }
            },
            onmousedown: move |_| pressed_backdrop.set(true),
            onclick: move |_| {
                if pressed_backdrop() {
                    cancel();
                }
                pressed_backdrop.set(false);
            },

            div {
                class: "confirm-window",
                onmousedown: move |evt| evt.stop_propagation(),
                onclick: move |evt| evt.stop_propagation(),

                h2 { class: "confirm-title", "{title}" }
                p { class: "confirm-message", "{message}" }
                if let Some(error) = error {
                    p { class: "confirm-error", "{error}" }
                }
                div { class: "confirm-buttons",
                    button {
                        class: "confirm-button confirm-cancel",
                        r#type: "button",
                        disabled: busy,
                        onmounted: move |evt| async move {
                            let _ = evt.set_focus(true).await;
                        },
                        onclick: move |_| cancel(),
                        {t!("common-cancel")}
                    }
                    button {
                        class: "confirm-button confirm-danger",
                        r#type: "button",
                        disabled: busy,
                        onclick: move |_| on_confirm.call(()),
                        if busy {
                            "{busy_label}"
                        } else {
                            "{confirm_label}"
                        }
                    }
                }
            }
        }
    }
}
