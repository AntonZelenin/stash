use dioxus::prelude::*;

use crate::AuthSession;
use crate::icons::{
    IconArrowUp, IconHelp, IconImage, IconLogout, IconMenu, IconSliders, IconStash, IconUser,
};
use crate::routes::Route;

const IMAGE_UPLOAD_INPUT_ID: &str = "home-image-upload-input";

/// Guesses a MIME type from a file name's extension. The browser normally
/// supplies this via the file input's `File.type`, but Dioxus's
/// cross-platform `FileEngine` only exposes file names, so this is
/// reconstructed client-side. The backend re-derives the real content type
/// from the file's bytes and does not trust this value.
fn guess_content_type(file_name: &str) -> &'static str {
    let lower = file_name.to_ascii_lowercase();
    if lower.ends_with(".png") {
        "image/png"
    } else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        "image/jpeg"
    } else if lower.ends_with(".gif") {
        "image/gif"
    } else if lower.ends_with(".webp") {
        "image/webp"
    } else {
        "application/octet-stream"
    }
}

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

    let submit = {
        let session = session.clone();
        move || {
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
        }
    };

    let upload_image = move |evt: FormEvent| {
        let session = session.clone();
        async move {
            if is_submitting() {
                return;
            }

            let Some(file) = evt.files().into_iter().next() else {
                return;
            };
            let file_name = file.name();
            let Ok(data) = file.read_bytes().await else {
                status.set(Some("Could not read the selected file".to_string()));
                return;
            };

            is_submitting.set(true);
            status.set(None);

            let content_type = guess_content_type(&file_name);
            if let Err(err) = session
                .create_image_item(&file_name, content_type, data.to_vec())
                .await
            {
                status.set(Some(err.to_string()));
            }

            is_submitting.set(false);
        }
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
                        input {
                            r#type: "file",
                            id: IMAGE_UPLOAD_INPUT_ID,
                            class: "home-image-input",
                            accept: "image/png,image/jpeg,image/gif,image/webp",
                            disabled: is_submitting(),
                            onchange: upload_image,
                        }
                        label {
                            class: "home-input-attach",
                            r#for: IMAGE_UPLOAD_INPUT_ID,
                            title: "Upload an image",
                            IconImage {}
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
