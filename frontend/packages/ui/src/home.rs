use api::ListedItem;
use base64::prelude::{BASE64_STANDARD, Engine as _};
use dioxus::html::{FileData, HasFileData};
use dioxus::prelude::*;

use crate::AuthSession;
use crate::icons::{
    IconArrowUp, IconClose, IconHelp, IconImage, IconLogout, IconMenu, IconSearch, IconSliders,
    IconStash, IconUser,
};
use crate::routes::Route;

const IMAGE_UPLOAD_INPUT_ID: &str = "home-image-upload-input";

/// Which of the filter chips is active. Filtering happens purely
/// client-side over the already-fetched page of items — there's no
/// separate "filtered" fetch.
#[derive(Clone, Copy, PartialEq)]
enum ItemFilter {
    All,
    Links,
    Images,
    Notes,
}

impl ItemFilter {
    /// `item_type` is the backend's raw `type` string (`"text"`, `"link"`,
    /// `"image"`) — "Notes" is this UI's name for a plain `"text"` item.
    fn matches(self, item_type: &str) -> bool {
        match self {
            ItemFilter::All => true,
            ItemFilter::Links => item_type == "link",
            ItemFilter::Images => item_type == "image",
            ItemFilter::Notes => item_type == "text",
        }
    }

    fn label(self) -> &'static str {
        match self {
            ItemFilter::All => "All",
            ItemFilter::Links => "Links",
            ItemFilter::Images => "Images",
            ItemFilter::Notes => "Notes",
        }
    }
}

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

    // Fetches once on mount (the closure captures no reactive state, so
    // `use_resource` never re-runs it on its own past that). `submit`
    // below calls `.restart()` after a successful save so new items show
    // up without a manual page reload. No pagination yet.
    let mut saved_items = use_resource({
        let session = session.clone();
        move || {
            let session = session.clone();
            async move { session.list_items(None, 30).await }
        }
    });

    let mut active_filter = use_signal(|| ItemFilter::All);
    let mut note = use_signal(String::new);
    let mut is_submitting = use_signal(|| false);
    let mut status = use_signal(|| None::<String>);
    // Counts nested dragenter/dragleave pairs rather than a bool: the
    // pointer crossing from the container into a child element fires
    // dragleave-then-dragenter for that child, and only the count (not a
    // single flag) tells the container it's still being dragged over.
    let mut drag_depth = use_signal(|| 0i32);
    let mut pending_images = use_signal(Vec::<PendingImage>::new);

    let mut submit = {
        let session = session.clone();
        move || {
            if is_submitting() {
                return;
            }

            // Staged images take priority: send those, and leave the note
            // text (if any) for the next send rather than silently dropping
            // it.
            let images = std::mem::take(&mut *pending_images.write());
            if !images.is_empty() {
                let session = session.clone();
                spawn(async move {
                    is_submitting.set(true);
                    status.set(None);

                    // One failure shouldn't stop the rest from uploading;
                    // report the last error, if any, once all are done.
                    let mut last_error = None;
                    for image in images {
                        if let Err(err) = session
                            .create_image_item(&image.file_name, image.content_type, image.data)
                            .await
                        {
                            last_error = Some(err.to_string());
                        }
                    }
                    status.set(last_error);
                    saved_items.restart();

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
                    Ok(_) => {
                        note.set(String::new());
                        saved_items.restart();
                    }
                    Err(err) => status.set(Some(err.to_string())),
                }

                is_submitting.set(false);
            });
        }
    };

    let stage_picked_files = move |evt: FormEvent| async move {
        if is_submitting() {
            return;
        }
        let files = evt.files();
        if files.is_empty() {
            return;
        }
        status.set(None);
        for file in files {
            match stage_file(file).await {
                Some(image) => pending_images.write().push(image),
                None => status.set(Some("Could not read the selected file".to_string())),
            }
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
        let files = evt.files();
        if files.is_empty() {
            return;
        }
        spawn(async move {
            status.set(None);
            for file in files {
                match stage_file(file).await {
                    Some(image) => pending_images.write().push(image),
                    None => status.set(Some("Could not read the selected file".to_string())),
                }
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

            div { class: "home-hero",
                IconStash {}
                h1 { class: "home-title", "stash" }
                p { class: "home-tagline", "Save anything. Find anytime." }

                if drag_depth() > 0 {
                    div { class: "home-drop-hint", "Drop image to upload" }
                }

                form {
                    class: "home-input-wrap",
                    onsubmit: move |evt| {
                        evt.prevent_default();
                        submit();
                    },
                    div { class: "home-input-inner",
                        // Staged images render inside the same card as the
                        // text row, not above or outside it — visually part
                        // of the message that Send is about to submit,
                        // rather than a floating block that leaves the
                        // input looking empty. Laid out 3-per-row so a
                        // large batch doesn't become one long scrolling
                        // line.
                        if !pending_images().is_empty() {
                            div { class: "home-image-preview-grid",
                                for (index , image) in pending_images().into_iter().enumerate() {
                                    div {
                                        class: "home-image-preview",
                                        key: "{index}-{image.file_name}",
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
                                            onclick: move |_| {
                                                pending_images.write().remove(index);
                                            },
                                            IconClose {}
                                        }
                                    }
                                }
                            }
                        }
                        div { class: "home-input-row",
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
                                multiple: true,
                                disabled: is_submitting(),
                                onchange: stage_picked_files,
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
                }

                if let Some(message) = status() {
                    p { class: "home-status", "{message}" }
                }
            }

            // Divider marks the hand-off from "save new content" (the hero
            // above) to "search existing content" (the workspace below) —
            // a sibling of both, not nested in either, so its width is its
            // own rather than inherited from the narrow hero or the wider
            // workspace container.
            hr { class: "home-divider" }

            // Full-width workspace band (a sibling of the narrow hero, not
            // nested in it) so it can span the whole page and grow to fill
            // the rest of the viewport, with only the content inside it
            // kept to a centered — but wider-than-the-hero — reading width.
            div { class: "stash-main",
                div { class: "stash-section",
                    // The search field and filter chips are still
                    // presentational only — actual search/filtering isn't
                    // wired up yet. The saved-items area below them is live.
                    div { class: "stash-search-wrap",
                        IconSearch {}
                        input {
                            class: "stash-search-input",
                            r#type: "search",
                            placeholder: "Search your stash...",
                        }
                    }

                    div { class: "stash-filters",
                        for filter in [ItemFilter::All, ItemFilter::Links, ItemFilter::Images, ItemFilter::Notes] {
                            button {
                                class: if active_filter() == filter { "filter-chip filter-chip-active" } else { "filter-chip" },
                                r#type: "button",
                                onclick: move |_| active_filter.set(filter),
                                "{filter.label()}"
                            }
                        }
                    }

                    {match &*saved_items.read() {
                        None => rsx! {
                            div { class: "stash-empty",
                                p { "Loading your stash..." }
                            }
                        },
                        Some(Err(err)) => rsx! {
                            div { class: "stash-empty",
                                p { "Could not load your stash: {err}" }
                            }
                        },
                        Some(Ok(response)) if response.items.is_empty() => rsx! {
                            div { class: "stash-empty",
                                IconStash {}
                                p { "Nothing saved yet — items you capture will show up here." }
                            }
                        },
                        Some(Ok(response)) => {
                            let filter = active_filter();
                            let filtered: Vec<ListedItem> = response
                                .items
                                .iter()
                                .filter(|item| filter.matches(&item.r#type))
                                .cloned()
                                .collect();

                            if filtered.is_empty() {
                                rsx! {
                                    div { class: "stash-empty",
                                        p { "No {filter.label().to_lowercase()} yet." }
                                    }
                                }
                            } else {
                                rsx! {
                                    div { class: "stash-grid",
                                        for item in filtered {
                                            ItemCard { key: "{item.id}", item }
                                        }
                                    }
                                }
                            }
                        }
                    }}
                }
            }
        }
    }
}

/// One saved item in the workspace grid. Renders the image for an image
/// item, or its text for a text item — whichever the item actually has.
#[component]
fn ItemCard(item: ListedItem) -> Element {
    rsx! {
        div { class: "item-card",
            if let Some(url) = &item.download_url {
                img { class: "item-card-image", src: "{url}", alt: "Saved image" }
            } else if let Some(text) = &item.text {
                p { class: "item-card-text", "{text}" }
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
