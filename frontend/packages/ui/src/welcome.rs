use std::rc::Rc;

use api::{CurrentUser, UserLimits};
use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::i18n::{Language, current_language};
use crate::icons::IconClose;

/// The modal's backdrop, ✕ and button, shared with the settings window.
const SETTINGS_CSS: Asset = asset!("/assets/styling/settings.css");
const WELCOME_CSS: Asset = asset!("/assets/styling/welcome.css");

/// The limits to welcome `user` with: `Some` while they haven't dismissed
/// the welcome, either on the server (`onboarding_completed`) or in this
/// session (`dismissed`, so it closes at once, before the server knows).
pub(crate) fn pending_welcome(user: Option<&CurrentUser>, dismissed: bool) -> Option<UserLimits> {
    user.filter(|user| !user.onboarding_completed && !dismissed)
        .map(|user| user.limits.clone())
}

/// The first-login welcome: what Stash does, and the limits that apply.
/// Like the settings window, the ✕ button, Escape and a click outside it
/// all close it; each calls `on_close`, which records it as seen.
#[component]
pub fn Welcome(limits: UserLimits, on_close: EventHandler<()>) -> Element {
    // Whether the current click started on the backdrop (see
    // `AccountSettings`).
    let mut pressed_backdrop = use_signal(|| false);
    let mut start_button = use_signal(|| None::<Rc<MountedData>>);
    let rows = limit_rows(&limits, current_language());

    // Focused on opening: nothing else has focus on a fresh page, and
    // Escape must reach the dialog. Enter then starts, too.
    use_effect(move || {
        if let Some(button) = start_button() {
            spawn(async move {
                let _ = button.set_focus(true).await;
            });
        }
    });

    rsx! {
        document::Link { rel: "stylesheet", href: SETTINGS_CSS }
        document::Link { rel: "stylesheet", href: WELCOME_CSS }

        div {
            class: "settings-overlay",
            role: "dialog",
            aria_modal: "true",
            aria_labelledby: "welcome-title",
            tabindex: "-1",
            onkeydown: move |evt| {
                if evt.key() == Key::Escape {
                    on_close.call(());
                }
            },
            onmousedown: move |_| pressed_backdrop.set(true),
            onclick: move |_| {
                if pressed_backdrop() {
                    on_close.call(());
                }
                pressed_backdrop.set(false);
            },

            div {
                class: "welcome-window",
                onmousedown: move |evt| evt.stop_propagation(),
                onclick: move |evt| evt.stop_propagation(),

                h2 { id: "welcome-title", class: "welcome-title", {t!("welcome-title")} }
                p { class: "welcome-body", {t!("welcome-body-save")} }
                p { class: "welcome-body", {t!("welcome-body-search")} }

                if !rows.is_empty() {
                    section { class: "welcome-limits",
                        h3 { class: "welcome-limits-title", {t!("welcome-limits-title")} }
                        dl { class: "welcome-limits-list",
                            for (label, value) in rows {
                                div { class: "welcome-limit",
                                    dt { "{label}" }
                                    dd { "{value}" }
                                }
                            }
                        }
                        p { class: "welcome-limits-note", {t!("welcome-limits-note")} }
                    }
                }

                div { class: "welcome-actions",
                    button {
                        class: "settings-button",
                        r#type: "button",
                        onmounted: move |evt| start_button.set(Some(evt.data())),
                        onclick: move |_| on_close.call(()),
                        {t!("welcome-start")}
                    }
                }

                button {
                    class: "settings-close",
                    r#type: "button",
                    title: t!("common-close"),
                    aria_label: t!("common-close"),
                    onclick: move |_| on_close.call(()),
                    IconClose {}
                }
            }
        }
    }
}

/// Each limit as (label, value) in `language`, leaving out daily quotas
/// the server doesn't enforce.
fn limit_rows(limits: &UserLimits, language: Language) -> Vec<(String, String)> {
    let count = |value: u64| language.format_integer(value);
    let mut rows = vec![
        (
            t!("welcome-limit-file-size"),
            format_limit_bytes(limits.max_file_bytes, language),
        ),
        (
            t!("welcome-limit-image-size"),
            format_limit_bytes(limits.max_image_bytes, language),
        ),
        (
            t!("welcome-limit-text-length"),
            t!(
                "welcome-limit-text-length-value",
                formatted: count(limits.max_text_length),
                count: limits.max_text_length
            ),
        ),
    ];
    if let Some(uploads) = limits.uploads_per_day {
        rows.push((t!("welcome-limit-uploads"), count(uploads)));
    }
    if let Some(bytes) = limits.upload_bytes_per_day {
        rows.push((
            t!("welcome-limit-upload-volume"),
            format_limit_bytes(bytes, language),
        ));
    }
    if let Some(analyses) = limits.ai_analyses_per_day {
        rows.push((t!("welcome-limit-ai-analyses"), count(analyses)));
    }
    rows
}

/// A limit in bytes in its largest whole-ish unit: "500 MB", "5 GB",
/// "1.5 GB" (binary units, as the item cards use).
fn format_limit_bytes(bytes: u64, language: Language) -> String {
    const KB: u64 = 1024;
    const MB: u64 = KB * 1024;
    const GB: u64 = MB * 1024;
    let in_unit = |unit: u64| {
        let decimals = if bytes.is_multiple_of(unit) { 0 } else { 1 };
        language.format_decimal(bytes as f64 / unit as f64, decimals)
    };
    if bytes >= GB {
        t!("size-gigabytes", size: in_unit(GB))
    } else if bytes >= MB {
        t!("size-megabytes", size: in_unit(MB))
    } else if bytes >= KB {
        t!("size-kilobytes", size: in_unit(KB))
    } else {
        t!("size-bytes", size: bytes.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::i18n::tests::in_language;

    const MB: u64 = 1024 * 1024;
    const GB: u64 = 1024 * MB;

    fn limits() -> UserLimits {
        UserLimits {
            max_file_bytes: 500 * MB,
            max_image_bytes: 100 * MB,
            max_text_length: 100_000,
            uploads_per_day: Some(1000),
            upload_bytes_per_day: Some(5 * GB),
            ai_analyses_per_day: Some(1000),
        }
    }

    fn user(onboarding_completed: bool) -> CurrentUser {
        CurrentUser {
            id: "u1".to_string(),
            email: "a@example.com".to_string(),
            onboarding_completed,
            limits: limits(),
        }
    }

    fn rows(limits: &UserLimits, language: Language) -> Vec<(String, String)> {
        // Fluent wraps placeables in bidi isolation marks; not what's
        // being checked here.
        in_language(language, || limit_rows(limits, language))
            .into_iter()
            .map(|(label, value)| (label, value.replace(['\u{2068}', '\u{2069}'], "")))
            .collect()
    }

    #[test]
    fn shown_to_a_user_who_has_not_completed_onboarding() {
        assert_eq!(pending_welcome(Some(&user(false)), false), Some(limits()));
    }

    #[test]
    fn not_shown_once_completed_on_the_server() {
        assert_eq!(pending_welcome(Some(&user(true)), false), None);
    }

    #[test]
    fn not_shown_again_once_dismissed_in_this_session() {
        assert_eq!(pending_welcome(Some(&user(false)), true), None);
    }

    #[test]
    fn not_shown_before_the_account_has_loaded() {
        assert_eq!(pending_welcome(None, false), None);
    }

    #[test]
    fn limits_in_english() {
        let expected = [
            ("Max file size", "500 MB"),
            ("Max image size", "100 MB"),
            ("Max text length", "100,000 characters"),
            ("Uploads per day", "1,000"),
            ("Upload volume per day", "5 GB"),
            ("AI analyses per day", "1,000"),
        ]
        .map(|(label, value)| (label.to_string(), value.to_string()));

        assert_eq!(rows(&limits(), Language::English), expected);
    }

    #[test]
    fn limits_in_ukrainian() {
        let expected = [
            ("Максимальний розмір файлу", "500 МБ"),
            ("Максимальний розмір зображення", "100 МБ"),
            ("Максимальна довжина тексту", "100\u{a0}000 символів"),
            ("Завантажень на день", "1\u{a0}000"),
            ("Обсяг завантажень на день", "5 ГБ"),
            ("Аналізів ШІ на день", "1\u{a0}000"),
        ]
        .map(|(label, value)| (label.to_string(), value.to_string()));

        assert_eq!(rows(&limits(), Language::Ukrainian), expected);
    }

    #[test]
    fn character_counts_are_pluralized_per_language() {
        let text_length = |language, max_text_length| {
            let limits = UserLimits {
                max_text_length,
                ..limits()
            };
            rows(&limits, language)[2].1.clone()
        };

        assert_eq!(text_length(Language::English, 1), "1 character");
        assert_eq!(text_length(Language::Ukrainian, 1), "1 символ");
        assert_eq!(text_length(Language::Ukrainian, 2), "2 символи");
        assert_eq!(text_length(Language::Ukrainian, 5), "5 символів");
    }

    #[test]
    fn quotas_the_server_does_not_enforce_are_left_out() {
        let limits = UserLimits {
            uploads_per_day: None,
            upload_bytes_per_day: None,
            ai_analyses_per_day: None,
            ..limits()
        };

        let labels: Vec<_> = rows(&limits, Language::English)
            .into_iter()
            .map(|(label, _)| label)
            .collect();

        assert_eq!(
            labels,
            ["Max file size", "Max image size", "Max text length"]
        );
    }

    #[test]
    fn sizes_use_the_largest_unit_and_the_languages_decimals() {
        let size = |bytes, language| {
            in_language(language, || format_limit_bytes(bytes, language))
                .replace(['\u{2068}', '\u{2069}'], "")
        };

        assert_eq!(size(3 * GB / 2, Language::English), "1.5 GB");
        assert_eq!(size(3 * GB / 2, Language::Ukrainian), "1,5 ГБ");
        assert_eq!(size(64 * 1024, Language::English), "64 KB");
        assert_eq!(size(512, Language::English), "512 B");
    }
}
