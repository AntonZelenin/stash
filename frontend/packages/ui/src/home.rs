use base64::prelude::{BASE64_STANDARD, Engine as _};
use dioxus::html::{FileData, HasFileData};
use dioxus::prelude::*;

use crate::AuthSession;
use crate::icons::{
    IconArrowUp, IconClose, IconHelp, IconImage, IconLogout, IconMenu, IconSliders, IconStash,
    IconUser,
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

/// An image the user has picked or dropped but not yet sent. Holds a
/// `data:` URL rather than an object URL for the preview so it works the
/// same way on every platform `ui` supports, not just the browser.
#[derive(Clone, PartialEq)]
struct PendingImage {
    file_name: String,
    content_type: &'static str,
    data: Vec<u8>,
    preview_url: String,
}

/// Reads `file` into a `PendingImage` ready for preview, without uploading
/// it. Shared by both the file-picker input and drag-and-drop, which differ
/// only in how they obtain the `FileData`.
async fn stage_file(file: FileData) -> Option<PendingImage> {
    let file_name = file.name();
    let data = file.read_bytes().await.ok()?.to_vec();
    let content_type = guess_content_type(&file_name);
    let preview_url = format!(
        "data:{content_type};base64,{}",
        BASE64_STANDARD.encode(&data)
    );

    Some(PendingImage {
        file_name,
        content_type,
        data,
        preview_url,
    })
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
    // Counts nested dragenter/dragleave pairs rather than a bool: the
    // pointer crossing from the container into a child element fires
    // dragleave-then-dragenter for that child, and only the count (not a
    // single flag) tells the container it's still being dragged over.
    let mut drag_depth = use_signal(|| 0i32);
    let mut pending_image = use_signal(|| None::<PendingImage>);

    let mut submit = {
        let session = session.clone();
        move || {
            if is_submitting() {
                return;
            }

            // A staged image takes priority: send that, and leave the note
            // text (if any) for the next send rather than silently dropping
            // it.
            if let Some(image) = pending_image.write().take() {
                let session = session.clone();
                spawn(async move {
                    is_submitting.set(true);
                    status.set(None);

                    if let Err(err) = session
                        .create_image_item(&image.file_name, image.content_type, image.data)
                        .await
                    {
                        status.set(Some(err.to_string()));
                    }

                    is_submitting.set(false);
                });
                return;
            }

            if note().trim().is_empty() {
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

    let stage_picked_file = move |evt: FormEvent| async move {
        if is_submitting() {
            return;
        }
        let Some(file) = evt.files().into_iter().next() else {
            return;
        };
        match stage_file(file).await {
            Some(image) => {
                status.set(None);
                pending_image.set(Some(image));
            }
            None => status.set(Some("Could not read the selected file".to_string())),
        }
    };

    let handle_drop = move |evt: DragEvent| {
        // Must run synchronously, before any `.await`, or the browser's
        // default action (opening the dropped file) fires first.
        evt.prevent_default();
        drag_depth.set(0);

        if is_submitting() {
            return;
        }
        let Some(file) = evt.files().into_iter().next() else {
            return;
        };
        spawn(async move {
            match stage_file(file).await {
                Some(image) => {
                    status.set(None);
                    pending_image.set(Some(image));
                }
                None => status.set(Some("Could not read the selected file".to_string())),
            }
        });
    };

    rsx! {
        document::Link { rel: "stylesheet", href: HOME_CSS }

        div {
            class: if drag_depth() > 0 { "home home-dragging" } else { "home" },
            ondragenter: move |evt| {
                evt.prevent_default();
                drag_depth += 1;
            },
            ondragleave: move |evt| {
                evt.prevent_default();
                drag_depth -= 1;
            },
            // Required for `ondrop` to fire at all — browsers reject drops
            // on elements that don't cancel dragover's default action.
            ondragover: move |evt| evt.prevent_default(),
            ondrop: handle_drop,

            TopBar {}

            div { class: "home-center",
                IconStash {}
                h1 { class: "home-title", "stash" }
                p { class: "home-tagline", "Save anything. Find anytime." }

                if drag_depth() > 0 {
                    div { class: "home-drop-hint", "Drop image to upload" }
                }

                if let Some(image) = pending_image() {
                    div { class: "home-image-preview",
                        img {
                            class: "home-image-preview-thumb",
                            src: "{image.preview_url}",
                            alt: "{image.file_name}",
                        }
                        span { class: "home-image-preview-name", "{image.file_name}" }
                        button {
                            class: "home-image-preview-remove",
                            r#type: "button",
                            title: "Remove image",
                            disabled: is_submitting(),
                            onclick: move |_| pending_image.set(None),
                            IconClose {}
                        }
                    }
                }

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
                            onchange: stage_picked_file,
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
