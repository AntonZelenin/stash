use dioxus::prelude::*;

use crate::icons::{IconEye, IconEyeOff, IconInbox, IconLock, IconMail};

const AUTH_CSS: Asset = asset!("/assets/styling/auth.css");

#[derive(Clone, Copy, PartialEq)]
enum AuthTab {
    Login,
    Signup,
}

#[component]
pub fn Auth() -> Element {
    let mut tab = use_signal(|| AuthTab::Login);

    rsx! {
        document::Link { rel: "stylesheet", href: AUTH_CSS }

        div { class: "auth-page",
            div { class: "auth-brand",
                IconInbox {}
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

#[component]
fn LoginForm() -> Element {
    let mut email = use_signal(String::new);
    let mut password = use_signal(String::new);
    let mut show_password = use_signal(|| false);

    rsx! {
        form {
            class: "auth-form",
            onsubmit: move |evt| evt.prevent_default(),

            div { class: "input-group",
                IconMail {}
                input {
                    class: "input-field",
                    r#type: "email",
                    placeholder: "Email",
                    value: "{email}",
                    oninput: move |evt| email.set(evt.value()),
                }
            }

            div { class: "input-group",
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Password",
                    value: "{password}",
                    oninput: move |evt| password.set(evt.value()),
                }
                button {
                    class: "input-icon-button",
                    r#type: "button",
                    onclick: move |_| show_password.set(!show_password()),
                    if show_password() { IconEyeOff {} } else { IconEye {} }
                }
            }

            button { class: "btn-primary", r#type: "submit", "Log in" }

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
    let mut email = use_signal(String::new);
    let mut password = use_signal(String::new);
    let mut confirm_password = use_signal(String::new);
    let mut show_password = use_signal(|| false);

    rsx! {
        form {
            class: "auth-form",
            onsubmit: move |evt| evt.prevent_default(),

            div { class: "input-group",
                IconMail {}
                input {
                    class: "input-field",
                    r#type: "email",
                    placeholder: "Email",
                    value: "{email}",
                    oninput: move |evt| email.set(evt.value()),
                }
            }

            div { class: "input-group",
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Password",
                    value: "{password}",
                    oninput: move |evt| password.set(evt.value()),
                }
                button {
                    class: "input-icon-button",
                    r#type: "button",
                    onclick: move |_| show_password.set(!show_password()),
                    if show_password() { IconEyeOff {} } else { IconEye {} }
                }
            }

            div { class: "input-group",
                IconLock {}
                input {
                    class: "input-field",
                    r#type: if show_password() { "text" } else { "password" },
                    placeholder: "Confirm password",
                    value: "{confirm_password}",
                    oninput: move |evt| confirm_password.set(evt.value()),
                }
            }

            button { class: "btn-primary", r#type: "submit", "Sign up" }
        }
    }
}
