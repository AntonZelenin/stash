use dioxus::prelude::*;

use crate::AuthSession;
use crate::icons::{IconArrowUp, IconHelp, IconLogout, IconMenu, IconSliders, IconStash, IconUser};
use crate::routes::Route;

const HOME_CSS: Asset = asset!("/assets/styling/home.css");

#[component]
pub fn Home() -> Element {
    let session = use_context::<AuthSession>();
    let nav = use_navigator();

    {
        let session = session.clone();
        use_effect(move || {
            if !session.is_authenticated() {
                nav.push(Route::Auth {});
            }
        });
    }

    let mut note = use_signal(String::new);
    let mut is_submitting = use_signal(|| false);
    let mut status = use_signal(|| None::<String>);

    let submit = move || {
        if is_submitting() || note().trim().is_empty() {
            return;
        }

        let session = session.clone();
        spawn(async move {
            is_submitting.set(true);
            status.set(None);

            match session.create_text_item(note().trim()).await {
                Ok(_) => note.set(String::new()),
                Err(err) => status.set(Some(err.to_string())),
            }

            is_submitting.set(false);
        });
    };

    rsx! {
        document::Link { rel: "stylesheet", href: HOME_CSS }

        div { class: "home",
            TopBar {}

            div { class: "home-center",
                IconStash {}
                h1 { class: "home-title", "stash" }
                p { class: "home-tagline", "Save anything. Find anytime." }

                form {
                    class: "home-input-wrap",
                    onsubmit: move |evt| {
                        evt.prevent_default();
                        submit();
                    },
                    div { class: "home-input-inner",
                        input {
                            class: "home-input",
                            placeholder: "Paste a link, write a note, or anything...",
                            value: "{note}",
                            oninput: move |evt| note.set(evt.value()),
                        }
                        button {
                            class: "home-input-submit",
                            r#type: "submit",
                            disabled: is_submitting(),
                            IconArrowUp {}
                        }
                    }
                }

                if let Some(message) = status() {
                    p { class: "home-status", "{message}" }
                }
            }
        }
    }
}

#[component]
fn TopBar() -> Element {
    let session = use_context::<AuthSession>();
    let mut menu_open = use_signal(|| false);

    rsx! {
        header { class: "top-bar",
            div { class: "top-bar-brand",
                IconStash {}
                span { class: "top-bar-name", "stash" }
            }

            div { class: "top-bar-menu",
                button {
                    class: "menu-button",
                    r#type: "button",
                    onclick: move |_| menu_open.set(!menu_open()),
                    div { class: "menu-button-inner", IconMenu {} }
                }

                if menu_open() {
                    div { class: "menu-dropdown",
                        button { class: "menu-item", r#type: "button", IconUser {} "Account settings" }
                        button { class: "menu-item", r#type: "button", IconSliders {} "Preferences" }
                        button { class: "menu-item", r#type: "button", IconHelp {} "Help & feedback" }
                        div { class: "menu-divider" }
                        button {
                            class: "menu-item menu-item-danger",
                            r#type: "button",
                            onclick: move |_| session.logout(),
                            IconLogout {}
                            "Logout"
                        }
                    }
                }
            }
        }
    }
}
