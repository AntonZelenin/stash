//! Asking what to do with files about to be uploaded that look like ones
//! the user has already saved, or like an earlier file of the same upload.
//!
//! A duplicate is a file with the same type (image or file), exact
//! filename (so the same extension) and exact size in bytes. Content is
//! never read or hashed, so the check is instant whatever the size: the
//! server says which files some of the user's items match
//! (`AuthSession::find_duplicates`), grouped per file; within the batch,
//! the first file with that metadata is the primary one and later ones are
//! its duplicates. Uploading one anyway is allowed: it becomes a new item
//! under its own, unchanged filename.

use api::{DuplicateGroup, UploadCandidate};
use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::items::full_local_date;

const CONFIRM_CSS: Asset = asset!("/assets/styling/confirm.css");
const DUPLICATES_CSS: Asset = asset!("/assets/styling/duplicates.css");

/// What a file about to be uploaded duplicates: saved items with its
/// metadata (`stored`), and/or an earlier file of the same batch with it
/// (`first_in_batch`, that file's position). At least one of them.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Duplicate {
    pub stored: Option<DuplicateGroup>,
    pub first_in_batch: Option<usize>,
}

/// Whether `group` is the server's answer about `file`: the same type,
/// filename (exactly as asked) and size.
fn describes(group: &DuplicateGroup, file: &UploadCandidate) -> bool {
    group.upload_type == file.upload_type
        && group.file_name == file.file_name
        && group.size_bytes == file.size_bytes
}

/// Which of `files` (about to be uploaded, in order) are duplicates, with
/// their positions: every file some saved items match (`groups`, from the
/// server), and every file after the first one of the batch with the same
/// type, filename and size. The first such file is the primary one: it's a
/// duplicate only if saved items match it. A file that's both is one entry
/// with both.
pub(crate) fn duplicate_positions(
    files: &[UploadCandidate],
    groups: &[DuplicateGroup],
) -> Vec<(usize, Duplicate)> {
    files
        .iter()
        .enumerate()
        .filter_map(|(position, file)| {
            let stored = groups.iter().find(|group| describes(group, file)).cloned();
            let first_in_batch = files[..position].iter().position(|earlier| earlier == file);
            (stored.is_some() || first_in_batch.is_some()).then_some((
                position,
                Duplicate {
                    stored,
                    first_in_batch,
                },
            ))
        })
        .collect()
}

/// A row of `DuplicateDialog`: a file about to be uploaded, the saved
/// items matching it if any, and the name of the earlier file in the
/// batch with it if any.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct DuplicateFile {
    pub file_name: String,
    pub stored: Option<DuplicateGroup>,
    pub same_as_in_batch: Option<String>,
}

/// Asks what to do with each of `files`: skip it, or upload another copy.
/// One file gets the two choices as buttons. Several get one row each
/// (name, how many copies are saved and when the first was, and/or which
/// file of this upload it's the same as) with its own
/// choice, preset to skip, and "Skip all" / "Upload all copies" to set
/// every row at once, after which rows can still be changed; Continue
/// applies them.
///
/// `on_resolve` gets, for each of `files` in order, whether to upload it.
/// Cancel, Escape or a click outside calls `on_cancel` (nothing uploads,
/// the files stay staged).
#[component]
pub(crate) fn DuplicateDialog(
    files: Vec<DuplicateFile>,
    on_resolve: EventHandler<Vec<bool>>,
    on_cancel: EventHandler<()>,
) -> Element {
    let count = files.len();
    let mut upload = use_signal(|| vec![false; count]);
    // Whether the current click started on the backdrop (see `ConfirmDialog`).
    let mut pressed_backdrop = use_signal(|| false);
    let title = t!("duplicates-title", count: files.len());

    rsx! {
        document::Link { rel: "stylesheet", href: CONFIRM_CSS }
        document::Link { rel: "stylesheet", href: DUPLICATES_CSS }

        div {
            class: "confirm-overlay",
            role: "alertdialog",
            aria_modal: "true",
            aria_label: "{title}",
            tabindex: "-1",
            onkeydown: move |evt| {
                if evt.key() == Key::Escape {
                    evt.stop_propagation();
                    on_cancel.call(());
                }
            },
            onmousedown: move |_| pressed_backdrop.set(true),
            onclick: move |_| {
                if pressed_backdrop() {
                    on_cancel.call(());
                }
                pressed_backdrop.set(false);
            },

            div {
                class: "confirm-window duplicates-window",
                onmousedown: move |evt| evt.stop_propagation(),
                onclick: move |evt| evt.stop_propagation(),

                h2 { class: "confirm-title", "{title}" }

                if let [file] = files.as_slice() {
                    p { class: "confirm-message",
                        if file.stored.is_some() {
                            {t!("duplicates-message-one", name: file.file_name.as_str())}
                        } else {
                            {t!("duplicates-message-one-in-batch", name: file.file_name.as_str())}
                        }
                    }
                    DuplicateSummary { file: file.clone(), with_last: true }
                    div { class: "confirm-buttons",
                        button {
                            class: "confirm-button confirm-cancel",
                            r#type: "button",
                            onmounted: move |evt| async move {
                                let _ = evt.set_focus(true).await;
                            },
                            onclick: move |_| on_resolve.call(vec![false]),
                            {t!("duplicates-skip")}
                        }
                        button {
                            class: "confirm-button duplicates-primary",
                            r#type: "button",
                            onclick: move |_| on_resolve.call(vec![true]),
                            {t!("duplicates-upload-another")}
                        }
                    }
                } else {
                    p { class: "confirm-message", {t!("duplicates-message-many")} }
                    div { class: "duplicates-bulk",
                        button {
                            class: "duplicates-bulk-button",
                            r#type: "button",
                            onclick: move |_| upload.set(vec![false; count]),
                            {t!("duplicates-skip-all")}
                        }
                        button {
                            class: "duplicates-bulk-button",
                            r#type: "button",
                            onclick: move |_| upload.set(vec![true; count]),
                            {t!("duplicates-upload-all")}
                        }
                    }
                    ul { class: "duplicates-list",
                        for (index , file) in files.iter().enumerate() {
                            li { class: "duplicates-row", key: "{index}",
                                div { class: "duplicates-row-info",
                                    span {
                                        class: "duplicates-row-name",
                                        title: "{file.file_name}",
                                        "{file.file_name}"
                                    }
                                    DuplicateSummary { file: file.clone(), with_last: false }
                                }
                                div {
                                    class: "duplicates-choice",
                                    role: "radiogroup",
                                    aria_label: "{file.file_name}",
                                    ChoiceButton {
                                        label: t!("duplicates-skip"),
                                        selected: !upload()[index],
                                        on_pick: move |_| upload.write()[index] = false,
                                    }
                                    ChoiceButton {
                                        label: t!("duplicates-upload-copy"),
                                        selected: upload()[index],
                                        on_pick: move |_| upload.write()[index] = true,
                                    }
                                }
                            }
                        }
                    }
                    div { class: "confirm-buttons",
                        button {
                            class: "confirm-button confirm-cancel",
                            r#type: "button",
                            onclick: move |_| on_cancel.call(()),
                            {t!("common-cancel")}
                        }
                        button {
                            class: "confirm-button duplicates-primary",
                            r#type: "button",
                            onmounted: move |evt| async move {
                                let _ = evt.set_focus(true).await;
                            },
                            onclick: move |_| on_resolve.call(upload()),
                            {t!("duplicates-continue")}
                        }
                    }
                }
            }
        }
    }
}

/// What a file duplicates: how many copies of its content are saved and
/// when the first was (and, `with_last`, the most recent, when there are
/// several), and which earlier file of this upload has the same content.
#[component]
fn DuplicateSummary(file: DuplicateFile, with_last: bool) -> Element {
    let count = file.stored.as_ref().map(|group| group.count);
    let first = file
        .stored
        .as_ref()
        .and_then(|group| full_local_date(&group.first_created_at));
    let last = file
        .stored
        .as_ref()
        .filter(|group| with_last && group.count > 1)
        .and_then(|group| full_local_date(&group.last_created_at));
    rsx! {
        div { class: "duplicates-summary",
            if let Some(count) = count {
                span { {t!("duplicates-copies", count: count)} }
            }
            if let Some(date) = first {
                span { {t!("duplicates-first-saved", date: date.as_str())} }
            }
            if let Some(date) = last {
                span { {t!("duplicates-last-saved", date: date.as_str())} }
            }
            if let Some(name) = file.same_as_in_batch {
                span { {t!("duplicates-same-in-batch", name: name.as_str())} }
            }
        }
    }
}

/// One side of a row's Skip / Upload copy switch.
#[component]
fn ChoiceButton(label: String, selected: bool, on_pick: EventHandler<()>) -> Element {
    rsx! {
        button {
            class: if selected { "duplicates-choice-button duplicates-choice-selected" } else { "duplicates-choice-button" },
            r#type: "button",
            role: "radio",
            aria_checked: if selected { "true" } else { "false" },
            onclick: move |_| on_pick.call(()),
            "{label}"
        }
    }
}

#[cfg(test)]
mod tests {
    use api::UploadType;

    use super::*;

    fn file(name: &str, size_bytes: u64) -> UploadCandidate {
        UploadCandidate {
            upload_type: UploadType::File,
            file_name: name.to_string(),
            size_bytes,
        }
    }

    fn group(file: &UploadCandidate, count: u32) -> DuplicateGroup {
        DuplicateGroup {
            upload_type: file.upload_type,
            file_name: file.file_name.clone(),
            size_bytes: file.size_bytes,
            count,
            first_created_at: "2026-09-01T10:00:00Z".to_string(),
            last_created_at: "2026-09-20T10:00:00Z".to_string(),
        }
    }

    fn stored(group: DuplicateGroup) -> Duplicate {
        Duplicate {
            stored: Some(group),
            first_in_batch: None,
        }
    }

    fn in_batch(first: usize) -> Duplicate {
        Duplicate {
            stored: None,
            first_in_batch: Some(first),
        }
    }

    #[test]
    fn files_matching_saved_items_are_duplicates() {
        let report = file("report.pdf", 100);
        let photo = file("photo.jpg", 5);
        let groups = [group(&report, 2), group(&photo, 1)];

        let found = duplicate_positions(
            &[report.clone(), file("notes.txt", 7), photo.clone()],
            &groups,
        );

        assert_eq!(
            found,
            vec![
                (0, stored(group(&report, 2))),
                (2, stored(group(&photo, 1)))
            ]
        );
    }

    #[test]
    fn a_group_matches_only_the_same_type_name_and_size() {
        let report = file("report.pdf", 100);
        let groups = [group(&report, 1)];
        let as_image = UploadCandidate {
            upload_type: UploadType::Image,
            ..report.clone()
        };

        let found = duplicate_positions(
            &[
                file("Report.pdf", 100),
                file("report.pdf", 101),
                file("report.txt", 100),
                as_image,
            ],
            &groups,
        );

        assert!(found.is_empty());
    }

    #[test]
    fn later_files_repeating_one_in_the_batch_are_duplicates_of_the_first() {
        let a = file("a.pdf", 1);
        let b = file("b.pdf", 2);

        let found = duplicate_positions(
            &[
                a.clone(),
                b.clone(),
                a.clone(),
                a.clone(),
                b.clone(),
                // Same name, other size: not a repeat.
                file("a.pdf", 3),
            ],
            &[],
        );

        assert_eq!(
            found,
            vec![(2, in_batch(0)), (3, in_batch(0)), (4, in_batch(1))]
        );
    }

    #[test]
    fn a_file_both_saved_and_repeated_is_one_entry_with_both() {
        let a = file("a.pdf", 1);
        let groups = [group(&a, 3)];

        let found = duplicate_positions(&[a.clone(), file("b.pdf", 2), a.clone()], &groups);

        assert_eq!(
            found,
            vec![
                (0, stored(group(&a, 3))),
                (
                    2,
                    Duplicate {
                        stored: Some(group(&a, 3)),
                        first_in_batch: Some(0),
                    }
                ),
            ]
        );
    }

    #[test]
    fn distinct_unsaved_files_mean_no_duplicates() {
        assert!(duplicate_positions(&[file("a.pdf", 1), file("b.pdf", 1)], &[]).is_empty());
    }
}
