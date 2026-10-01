use std::rc::Rc;

use api::{ApiError, Tag};
use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::AuthSession;
use crate::auth::{MIN_PASSWORD_CHARS, validate_new_password};
use crate::confirm::ConfirmDialog;
use crate::filters::{FILTERS_CSS, FilterSection, FilterSectionOptions};
use crate::i18n::{Language, Localization, api_error_message};
use crate::icons::{IconClose, IconEyeOff, IconGlobe, IconLock, IconTrash};
use crate::preferences::{MAX_HIDDEN_TAGS, Preferences};

const SETTINGS_CSS: Asset = asset!("/assets/styling/settings.css");

/// A page of the settings window, listed in its side menu.
#[derive(Clone, Copy, PartialEq)]
enum Section {
    Password,
    Language,
    HiddenTags,
    DeleteAccount,
}

impl Section {
    // Deleting the account last, apart from the everyday settings.
    const ALL: [Section; 4] = [
        Section::Password,
        Section::Language,
        Section::HiddenTags,
        Section::DeleteAccount,
    ];

    fn label(self) -> String {
        match self {
            Section::Password => t!("settings-section-password"),
            Section::Language => t!("settings-section-language"),
            Section::HiddenTags => t!("settings-section-hidden-tags"),
            Section::DeleteAccount => t!("settings-section-delete-account"),
        }
    }
}

/// The account settings window: a modal over a dimmed backdrop, with the
/// sections listed on the left and the selected one shown on the right.
/// The ✕ button, Escape, and any click outside the window call `on_close`.
#[component]
pub fn AccountSettings(on_close: EventHandler<()>) -> Element {
    let mut section = use_signal(|| Section::Password);
    // Whether the current click started on the backdrop itself. A click
    // that starts inside the window and ends outside it (selecting text in
    // a field, say) is delivered to the backdrop too, but must not close.
    let mut pressed_backdrop = use_signal(|| false);

    rsx! {
        document::Link { rel: "stylesheet", href: SETTINGS_CSS }

        div {
            class: "settings-overlay",
            role: "dialog",
            aria_modal: "true",
            aria_label: t!("account-settings"),
            // Focusable, so Escape reaches it even after a click on the
            // window's non-interactive parts moves focus off the fields.
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
                class: "settings-window",
                onmousedown: move |evt| evt.stop_propagation(),
                onclick: move |evt| evt.stop_propagation(),

                nav { class: "settings-nav",
                    h2 { class: "settings-nav-title", {t!("settings-title")} }
                    for item in Section::ALL {
                        button {
                            class: if section() == item { "settings-nav-item active" } else { "settings-nav-item" },
                            r#type: "button",
                            aria_current: if section() == item { "page" } else { "false" },
                            onclick: move |_| section.set(item),
                            match item {
                                Section::Password => rsx! { IconLock {} },
                                Section::Language => rsx! { IconGlobe {} },
                                Section::HiddenTags => rsx! { IconEyeOff {} },
                                Section::DeleteAccount => rsx! { IconTrash {} },
                            }
                            "{item.label()}"
                        }
                    }
                }

                div { class: "settings-main",
                    match section() {
                        Section::Password => rsx! {
                            PasswordSettings {}
                        },
                        Section::Language => rsx! {
                            LanguageSettings {}
                        },
                        Section::HiddenTags => rsx! {
                            HiddenTagsSettings {}
                        },
                        Section::DeleteAccount => rsx! {
                            DeleteAccountSettings {}
                        },
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

/// Change-password form: the current password, the new one twice.
#[component]
fn PasswordSettings() -> Element {
    let session = use_context::<AuthSession>();
    let mut current = use_signal(String::new);
    let mut new = use_signal(String::new);
    let mut confirm = use_signal(String::new);
    let mut current_error = use_signal(|| None::<String>);
    let mut new_error = use_signal(|| None::<String>);
    let mut confirm_error = use_signal(|| None::<String>);
    let mut general_error = use_signal(|| None::<String>);
    let mut saving = use_signal(|| false);
    let mut saved = use_signal(|| false);
    let mut current_input = use_signal(|| None::<Rc<MountedData>>);

    let mut clear_messages = move || {
        current_error.set(None);
        new_error.set(None);
        confirm_error.set(None);
        general_error.set(None);
        saved.set(false);
    };

    let submit = move |evt: FormEvent| {
        evt.prevent_default();
        if saving() {
            return;
        }
        clear_messages();

        let current_value = current();
        let new_value = new();
        let mut valid = true;
        if current_value.is_empty() {
            current_error.set(Some(t!("settings-current-password-missing")));
            valid = false;
        }
        if let Some(error) = validate_new_password(&new_value) {
            new_error.set(Some(error.message()));
            valid = false;
        } else if confirm() != new_value {
            confirm_error.set(Some(t!("auth-passwords-mismatch")));
            valid = false;
        }
        if !valid {
            return;
        }

        let session = session.clone();
        saving.set(true);
        spawn(async move {
            match session.change_password(&current_value, &new_value).await {
                Ok(()) => {
                    current.set(String::new());
                    new.set(String::new());
                    confirm.set(String::new());
                    saved.set(true);
                }
                Err(ApiError::Validation(errors)) => {
                    let mut general = Vec::new();
                    for error in errors {
                        match error.field.as_deref() {
                            Some("current_password") => current_error.set(Some(error.message)),
                            Some("new_password") => new_error.set(Some(error.message)),
                            _ => general.push(error.message),
                        }
                    }
                    if !general.is_empty() {
                        general_error.set(Some(general.join(" ")));
                    }
                    if current_error.read().is_some()
                        && let Some(input) = current_input()
                    {
                        let _ = input.set_focus(true).await;
                    }
                }
                Err(err) => general_error.set(Some(api_error_message(&err))),
            }
            saving.set(false);
        });
    };

    rsx! {
        h3 { class: "settings-title", {t!("settings-section-password")} }
        p { class: "settings-description", {t!("settings-password-description")} }

        form { class: "settings-form", novalidate: true, onsubmit: submit,
            if let Some(message) = general_error() {
                p { class: "settings-error", role: "alert", "{message}" }
            }
            if saved() {
                p { class: "settings-success", role: "status", {t!("settings-password-changed")} }
            }

            PasswordField {
                id: "settings-current-password",
                label: t!("settings-current-password"),
                autocomplete: "current-password",
                value: current(),
                error: current_error(),
                on_input: move |value| {
                    current.set(value);
                    current_error.set(None);
                    saved.set(false);
                },
                on_mounted: move |input: Rc<MountedData>| {
                    current_input.set(Some(input.clone()));
                    spawn(async move {
                        let _ = input.set_focus(true).await;
                    });
                },
            }
            PasswordField {
                id: "settings-new-password",
                label: t!("settings-new-password"),
                autocomplete: "new-password",
                hint: t!("settings-new-password-hint", min: MIN_PASSWORD_CHARS),
                value: new(),
                error: new_error(),
                on_input: move |value| {
                    new.set(value);
                    new_error.set(None);
                    saved.set(false);
                },
            }
            PasswordField {
                id: "settings-confirm-password",
                label: t!("settings-confirm-new-password"),
                autocomplete: "new-password",
                value: confirm(),
                error: confirm_error(),
                on_input: move |value| {
                    confirm.set(value);
                    confirm_error.set(None);
                    saved.set(false);
                },
            }

            div { class: "settings-actions",
                button {
                    class: "settings-button",
                    r#type: "submit",
                    disabled: saving(),
                    if saving() { {t!("common-saving")} } else { {t!("settings-change-password")} }
                }
            }
        }
    }
}

/// The UI language, chosen from every supported one (each listed by its
/// own name). Takes effect at once and is remembered on this device.
#[component]
fn LanguageSettings() -> Element {
    let localization = use_context::<Localization>();
    let current = localization.language();

    rsx! {
        h3 { class: "settings-title", {t!("settings-section-language")} }
        p { class: "settings-description", {t!("settings-language-description")} }

        div { class: "settings-form",
            div { class: "settings-field",
                label { class: "settings-label", r#for: "settings-language", {t!("settings-language-label")} }
                select {
                    id: "settings-language",
                    class: "settings-input",
                    value: current.code(),
                    onchange: move |evt| {
                        if let Some(language) = Language::from_tag(&evt.value()) {
                            localization.set_language(language);
                        }
                    },
                    for language in Language::ALL {
                        option {
                            value: language.code(),
                            lang: language.code(),
                            selected: language == current,
                            "{language.native_name()}"
                        }
                    }
                }
            }
        }
    }
}

/// The tags Blind mode hides: the user's tags as checkboxes (checked =
/// hidden), with the same search as the tag filter. Takes effect at once
/// and is remembered on this device, by tag id.
#[component]
fn HiddenTagsSettings() -> Element {
    let preferences = use_context::<Preferences>();
    let hidden = preferences.hidden_tag_ids();
    let at_limit = hidden.len() >= MAX_HIDDEN_TAGS;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        h3 { class: "settings-title", {t!("settings-section-hidden-tags")} }
        p { class: "settings-description", {t!("settings-hidden-tags-description")} }

        div { class: "settings-form settings-hidden-tags",
            FilterSectionOptions {
                section: FilterSection::Tags,
                selected: hidden,
                on_toggle: move |tag: Tag| preferences.toggle_hidden_tag(&tag.id),
            }
            if at_limit {
                p { class: "settings-hint", {t!("settings-hidden-tags-limit", max: MAX_HIDDEN_TAGS)} }
            }
        }
    }
}

/// Deleting the account: the password, then a confirmation, since it can't
/// be undone. Success signs out (the session's tokens die with the
/// account), which replaces the whole app, this window included, with the
/// login screen.
#[component]
fn DeleteAccountSettings() -> Element {
    let session = use_context::<AuthSession>();
    let mut password = use_signal(String::new);
    let mut password_error = use_signal(|| None::<String>);
    let mut confirming = use_signal(|| false);
    let mut deleting = use_signal(|| false);
    // A failure other than a wrong password, shown in the confirmation.
    let mut confirm_error = use_signal(|| None::<String>);
    let mut password_input = use_signal(|| None::<Rc<MountedData>>);

    let ask = move |evt: FormEvent| {
        evt.prevent_default();
        if password().is_empty() {
            password_error.set(Some(t!("settings-delete-account-password-missing")));
            return;
        }
        confirm_error.set(None);
        confirming.set(true);
    };

    let delete = move |_| {
        if deleting() {
            return;
        }
        let session = session.clone();
        let password_value = password();
        deleting.set(true);
        confirm_error.set(None);
        spawn(async move {
            match session.delete_account(&password_value).await {
                // Signed out: nothing here is shown any more.
                Ok(()) => return,
                Err(ApiError::Validation(errors)) => {
                    let messages: Vec<String> =
                        errors.into_iter().map(|error| error.message).collect();
                    confirming.set(false);
                    password_error.set(Some(messages.join(" ")));
                    if let Some(input) = password_input() {
                        let _ = input.set_focus(true).await;
                    }
                }
                Err(err) => confirm_error.set(Some(api_error_message(&err))),
            }
            deleting.set(false);
        });
    };

    rsx! {
        h3 { class: "settings-title", {t!("settings-section-delete-account")} }
        p { class: "settings-description", {t!("settings-delete-account-description")} }

        form { class: "settings-form", novalidate: true, onsubmit: ask,
            PasswordField {
                id: "settings-delete-account-password",
                label: t!("settings-delete-account-password"),
                autocomplete: "current-password",
                value: password(),
                error: password_error(),
                on_input: move |value| {
                    password.set(value);
                    password_error.set(None);
                },
                on_mounted: move |input: Rc<MountedData>| password_input.set(Some(input)),
            }

            div { class: "settings-actions",
                button { class: "settings-button settings-button-danger", r#type: "submit",
                    {t!("settings-delete-account")}
                }
            }
        }

        if confirming() {
            ConfirmDialog {
                title: t!("settings-delete-account-confirm-title"),
                message: t!("settings-delete-account-confirm-message"),
                confirm_label: t!("settings-delete-account-confirm"),
                busy_label: t!("settings-deleting-account"),
                busy: deleting(),
                error: confirm_error(),
                on_confirm: delete,
                on_cancel: move |_| confirming.set(false),
            }
        }
    }
}

/// A labelled password input with an optional hint and error below it.
#[component]
fn PasswordField(
    id: &'static str,
    label: String,
    autocomplete: &'static str,
    #[props(default)] hint: Option<String>,
    value: String,
    error: Option<String>,
    on_input: EventHandler<String>,
    #[props(default)] on_mounted: Option<EventHandler<Rc<MountedData>>>,
) -> Element {
    rsx! {
        div { class: "settings-field",
            label { class: "settings-label", r#for: id, "{label}" }
            input {
                id,
                class: if error.is_some() { "settings-input has-error" } else { "settings-input" },
                r#type: "password",
                autocomplete,
                value,
                aria_invalid: if error.is_some() { "true" } else { "false" },
                oninput: move |evt| on_input.call(evt.value()),
                onmounted: move |evt| {
                    if let Some(on_mounted) = on_mounted {
                        on_mounted.call(evt.data());
                    }
                },
            }
            if let Some(message) = &error {
                p { class: "settings-field-error", "{message}" }
            } else if let Some(hint) = hint {
                p { class: "settings-hint", "{hint}" }
            }
        }
    }
}
