//! Cloudflare Turnstile on the sign-up form: the backend only creates an
//! account for a registration that carries a token from this widget, which
//! it verifies with Cloudflare (see `app.turnstile` in the backend). The
//! widget is Cloudflare's own script, loaded on first use and driven
//! through `document::eval`.

use dioxus::document::Eval;
use dioxus::prelude::*;

/// The Turnstile site key the app was built with, provided as context by the
/// app (the web app's `STASH_TURNSTILE_SITE_KEY`). `None`, or no context at
/// all: no widget, and registrations carry no token (for a backend with
/// Turnstile off).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TurnstileSiteKey(pub Option<&'static str>);

const CONTAINER_ID: &str = "turnstile-widget";
/// The action tokens are solved for; the backend accepts only this one
/// (`app.turnstile.REGISTER_ACTION`).
const ACTION: &str = "register";

/// Loads Cloudflare's script once, renders the widget into the container,
/// and reports each change of its token as `[token, error]`. Then waits for
/// "reset" (a new token, the last one being spent) or "remove".
const SCRIPT: &str = r#"
const [containerId, siteKey, action] = await dioxus.recv();
const loaded = new Promise((resolve, reject) => {
  if (window.turnstile) { resolve(); return; }
  let script = document.getElementById("cf-turnstile-api");
  if (!script) {
    script = document.createElement("script");
    script.id = "cf-turnstile-api";
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    document.head.appendChild(script);
  }
  script.addEventListener("load", () => resolve());
  script.addEventListener("error", () => reject(new Error("Turnstile didn't load")));
});
try {
  await loaded;
} catch {
  dioxus.send([null, true]);
  return;
}
const container = document.getElementById(containerId);
if (!container) return;
const widget = window.turnstile.render(container, {
  sitekey: siteKey,
  action,
  callback: (token) => dioxus.send([token, false]),
  "expired-callback": () => dioxus.send([null, false]),
  "timeout-callback": () => dioxus.send([null, false]),
  "error-callback": () => dioxus.send([null, true]),
});
while (true) {
  const command = await dioxus.recv();
  if (command === "reset") {
    window.turnstile.reset(widget);
  } else if (command === "remove") {
    window.turnstile.remove(widget);
    return;
  }
}
"#;

/// The sign-up form's Turnstile widget: render it with `TurnstileWidget`,
/// send `token()` with the registration.
#[derive(Clone, Copy)]
pub(crate) struct Turnstile {
    enabled: bool,
    token: Signal<Option<String>>,
    failed: Signal<bool>,
    widget: Signal<Option<Eval>>,
}

impl Turnstile {
    /// Whether registrations need a token (the app has a site key).
    pub(crate) fn enabled(&self) -> bool {
        self.enabled
    }

    /// The current token: none until the widget is solved, and again once
    /// it expires or has been used.
    pub(crate) fn token(&self) -> Option<String> {
        (self.token)()
    }

    /// Whether the widget couldn't load or run (e.g. blocked, offline).
    pub(crate) fn failed(&self) -> bool {
        (self.failed)()
    }

    /// Tokens are single-use: after each registration attempt, the widget
    /// makes a new one.
    pub(crate) fn reset(&mut self) {
        self.token.set(None);
        if let Some(widget) = *self.widget.peek() {
            let _ = widget.send("reset");
        }
    }
}

pub(crate) fn use_turnstile() -> Turnstile {
    let site_key = try_use_context::<TurnstileSiteKey>().and_then(|key| key.0);
    let mut token = use_signal(|| None::<String>);
    let mut failed = use_signal(|| false);
    let mut widget = use_signal(|| None::<Eval>);

    // Once, after the container is in the page.
    use_effect(move || {
        let Some(site_key) = site_key else {
            return;
        };
        let mut eval = document::eval(SCRIPT);
        if eval.send((CONTAINER_ID, site_key, ACTION)).is_err() {
            failed.set(true);
            return;
        }
        widget.set(Some(eval));
        spawn(async move {
            while let Ok((new_token, error)) = eval.recv::<(Option<String>, bool)>().await {
                failed.set(error);
                token.set(new_token);
            }
        });
    });
    use_drop(move || {
        if let Some(widget) = *widget.peek() {
            let _ = widget.send("remove");
        }
    });

    Turnstile {
        enabled: site_key.is_some(),
        token,
        failed,
        widget,
    }
}

/// Where the widget renders.
#[component]
pub(crate) fn TurnstileWidget() -> Element {
    rsx! {
        div { id: CONTAINER_ID, class: "turnstile" }
    }
}
