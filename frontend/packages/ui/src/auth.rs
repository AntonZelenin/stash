use api::ApiError;
use dioxus::prelude::*;

use crate::AuthSession;
use crate::icons::{IconEye, IconEyeOff, IconLock, IconMail, IconStash};
use crate::routes::Route;

const AUTH_CSS: Asset = asset!("/assets/styling/auth.css");

#[derive(Clone, Copy, PartialEq)]
enum AuthTab {
    Login,
    Signup,
}

/// An `ApiError` split into what a login/signup form can show: a top-level
/// message, and messages tied to the specific fields the backend named.
struct FormErrors {
    general: Option<String>,
    email: Option<String>,
    password: Option<String>,
}

fn split_error(err: ApiError) -> FormErrors {
    match err {
        ApiError::Validation(field_errors) => {
            let mut result = FormErrors {
                general: None,
                email: None,
                password: None,
            };
            let mut general_messages = Vec::new();

            for field_error in field_errors {
                match field_error.field.as_deref() {
                    Some("email") => result.email = Some(field_error.message),
                    Some("password") => result.password = Some(field_error.message),
                    _ => general_messages.push(field_error.message),
                }
            }

            if !general_messages.is_empty() {
                result.general = Some(general_messages.join(" "));
            }

            result
        }
        other => FormErrors {
            general: Some(other.to_string()),
            email: None,
            password: None,
        },
    }
}

/// Must match the backend's `UserCreateRequest.password` limits.
const MIN_PASSWORD_CHARS: usize = 8;
const MAX_PASSWORD_CHARS: usize = 72;

/// A deliberately loose shape check — something@domain.tld, no spaces — for
/// instant feedback on obvious typos. The backend does the real validation,
/// and its verdict still lands on the email field if it disagrees.
fn validate_email(email: &str) -> Option<String> {
    let email = email.trim();
    if email.is_empty() {
        return Some("Enter your email".to_string());
    }
    let looks_valid = match email.split_once('@') {
        Some((local, domain)) => {
            !local.is_empty()
                && !domain.contains('@')
                && !email.chars().any(char::is_whitespace)
                && domain
                    .split_once('.')
                    .is_some_and(|(name, rest)| !name.is_empty() && !rest.is_empty())
                && !domain.ends_with('.')
        }
        None => false,
    };
    (!looks_valid).then(|| "Enter a valid email address".to_string())
}

/// Signup rules for a new password (login only requires one to be entered).
fn validate_new_password(password: &str) -> Option<String> {
    let length = password.chars().count();
    if length < MIN_PASSWORD_CHARS {
        Some(format!(
            "Password must be at least {MIN_PASSWORD_CHARS} characters"
        ))
    } else if length > MAX_PASSWORD_CHARS {
        Some(format!(
            "Password must be at most {MAX_PASSWORD_CHARS} characters"
        ))
    } else {
        None
    }
}

#[component]
pub fn Auth() -> Element {
    let session = use_context::<AuthSession>();
    let nav = use_navigator();

    // Covers both "redirect after a successful login/signup" and "don't
    // show the login form to an already-authenticated visitor": both cases
    // are just `session.tokens` becoming `Some`, which this reactively picks
    // up regardless of how it happened.
    use_effect(move || {
        if session.is_authenticated() {
            nav.push(Route::Home {});
        }
    });

    let mut tab = use_signal(|| AuthTab::Login);

    rsx! {
        document::Link { rel: "stylesheet", href: AUTH_CSS }

        div { class: "auth-page",
            div { class: "auth-content",
                div { class: "auth-brand",
                    IconStash {}
                    h1 { class: "brand-name", "stash" }
                    p { class: "brand-tagline",
                        "Save everything."
                        br {}
                        "Find it anytime."
                    }
                }

                div { class: "auth-card",
                    div { class: "auth-tabs",
                        button {
                            class: if tab() == AuthTab::Login { "auth-tab active" } else { "auth-tab" },
                            r#type: "button",
                            onclick: move |_| tab.set(AuthTab::Login),
                            "Log in"
                        }
                        button {
                            class: if tab() == AuthTab::Signup { "auth-tab active" } else { "auth-tab" },
                            r#type: "button",
                            onclick: move |_| tab.set(AuthTab::Signup),
                            "Sign up"
                        }
                    }

                    if tab() == AuthTab::Login {
                        LoginForm {}
                    } else {
                        SignupForm {}
                    }
                }
            }
        }
    }
}

#[component]
fn LoginForm() -> Element {
    let session = use_context::<AuthSession>();

    let mut email = use_signal(String::new);
    let mut password = use_signal(String::new);
    let mut show_password = use_signal(|| false);
    let mut general_error = use_signal(|| None::<String>);
    let mut email_error = use_signal(|| None::<String>);
    let mut password_error = use_signal(|| None::<String>);
    let mut is_submitting = use_signal(|| false);

    rsx! {
        form {
            class: "auth-form",
            // Our own validation shows errors on the fields; the browser's
            // built-in one (triggered by `type="email"`) would block submit
            // first with at most a native tooltip.
            novalidate: true,
            onsubmit: move |evt| {
                evt.prevent_default();
                if is_submitting() {
                    return;
                }

                general_error.set(None);
                email_error.set(validate_email(&email()));
                password_error.set(
                    password()
                        .is_empty()
                        .then(|| "Enter your password".to_string()),
                );
                if email_error().is_some() || password_error().is_some() {
                    return;
                }

                let session = session.clone();
                spawn(async move {
                    is_submitting.set(true);

                    if let Err(err) = session.login(email().trim(), &password()).await {
                        let errors = split_error(err);
                        general_error.set(errors.general);
                        email_error.set(errors.email);
                        password_error.set(errors.password);
                    }

                    is_submitting.set(false);
                });
            },

            if let Some(message) = general_error() {
                p { class: "auth-error", "{message}" }
            }

            div {
                class: if email_error().is_some() { "input-group has-error" } else { "input-group" },
                IconMail {}
                input {
                    class: "input-field",
                    r#type: "email",
                    placeholder: "Email",
                    value: "{email}",
                    oninput: move |evt| {
                        email.set(evt.value());
                        email_error.set(None);
                    },
                }
            }
            if let Some(message) = email_error() {
                p { class: "field-error", "{message}" }
            }

            div {
                class: if password_error().is_some() { "input-group has-error" } else { "input-group" },
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Password",
                    value: "{password}",
                    oninput: move |evt| {
                        password.set(evt.value());
                        password_error.set(None);
                    },
                }
                button {
                    class: "input-icon-button",
                    r#type: "button",
                    onclick: move |_| show_password.set(!show_password()),
                    if show_password() { IconEyeOff {} } else { IconEye {} }
                }
            }
            if let Some(message) = password_error() {
                p { class: "field-error", "{message}" }
            }

            button {
                class: "btn-primary",
                r#type: "submit",
                disabled: is_submitting(),
                if is_submitting() { "Logging in..." } else { "Log in" }
            }

            a {
                class: "link-forgot",
                href: "#",
                onclick: move |evt| evt.prevent_default(),
                "Forgot password?"
            }
        }
    }
}

#[component]
fn SignupForm() -> Element {
    let session = use_context::<AuthSession>();

    let mut email = use_signal(String::new);
    let mut password = use_signal(String::new);
    let mut confirm_password = use_signal(String::new);
    let mut show_password = use_signal(|| false);
    let mut general_error = use_signal(|| None::<String>);
    let mut email_error = use_signal(|| None::<String>);
    let mut password_error = use_signal(|| None::<String>);
    let mut confirm_password_error = use_signal(|| None::<String>);
    let mut is_submitting = use_signal(|| false);

    rsx! {
        form {
            class: "auth-form",
            // Our own validation shows errors on the fields; the browser's
            // built-in one (triggered by `type="email"`) would block submit
            // first with at most a native tooltip.
            novalidate: true,
            onsubmit: move |evt| {
                evt.prevent_default();
                if is_submitting() {
                    return;
                }

                general_error.set(None);
                email_error.set(validate_email(&email()));
                password_error.set(validate_new_password(&password()));
                confirm_password_error.set(
                    (password() != confirm_password())
                        .then(|| "Passwords don't match".to_string()),
                );
                if email_error().is_some()
                    || password_error().is_some()
                    || confirm_password_error().is_some()
                {
                    return;
                }

                let session = session.clone();
                spawn(async move {
                    is_submitting.set(true);

                    if let Err(err) = session.register(email().trim(), &password()).await {
                        let errors = split_error(err);
                        general_error.set(errors.general);
                        email_error.set(errors.email);
                        password_error.set(errors.password);
                    }

                    is_submitting.set(false);
                });
            },

            if let Some(message) = general_error() {
                p { class: "auth-error", "{message}" }
            }

            div {
                class: if email_error().is_some() { "input-group has-error" } else { "input-group" },
                IconMail {}
                input {
                    class: "input-field",
                    r#type: "email",
                    placeholder: "Email",
                    value: "{email}",
                    oninput: move |evt| {
                        email.set(evt.value());
                        email_error.set(None);
                    },
                }
            }
            if let Some(message) = email_error() {
                p { class: "field-error", "{message}" }
            }

            div {
                class: if password_error().is_some() { "input-group has-error" } else { "input-group" },
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Password",
                    value: "{password}",
                    oninput: move |evt| {
                        password.set(evt.value());
                        password_error.set(None);
                    },
                }
                button {
                    class: "input-icon-button",
                    r#type: "button",
                    onclick: move |_| show_password.set(!show_password()),
                    if show_password() { IconEyeOff {} } else { IconEye {} }
                }
            }
            if let Some(message) = password_error() {
                p { class: "field-error", "{message}" }
            }

            div {
                class: if confirm_password_error().is_some() { "input-group has-error" } else { "input-group" },
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Confirm password",
                    value: "{confirm_password}",
                    oninput: move |evt| {
                        confirm_password.set(evt.value());
                        confirm_password_error.set(None);
                    },
                }
            }
            if let Some(message) = confirm_password_error() {
                p { class: "field-error", "{message}" }
            }

            button {
                class: "btn-primary",
                r#type: "submit",
                disabled: is_submitting(),
                if is_submitting() { "Signing up..." } else { "Sign up" }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_plausible_emails() {
        for email in [
            "me@example.com",
            "first.last+tag@mail.example.co.uk",
            "  padded@example.com  ",
        ] {
            assert_eq!(validate_email(email), None, "{email}");
        }
    }

    #[test]
    fn rejects_obvious_email_typos() {
        assert_eq!(validate_email("   "), Some("Enter your email".to_string()));
        for email in [
            "not-an-email",
            "@example.com",
            "me@",
            "me@example",
            "me@.com",
            "me@example.",
            "me@@example.com",
            "me @example.com",
        ] {
            assert_eq!(
                validate_email(email),
                Some("Enter a valid email address".to_string()),
                "{email}"
            );
        }
    }

    #[test]
    fn new_password_length_matches_backend_limits() {
        assert!(validate_new_password("short").is_some());
        assert_eq!(validate_new_password("exactly8"), None);
        assert_eq!(validate_new_password(&"x".repeat(72)), None);
        assert!(validate_new_password(&"x".repeat(73)).is_some());
        // Counted in characters, like the backend, not bytes.
        assert_eq!(validate_new_password("пароль12"), None);
    }
}
