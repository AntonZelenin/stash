use dioxus::prelude::*;

use crate::routes::Route;
use crate::AuthSession;

#[component]
pub fn Home() -> Element {
    let session = use_context::<AuthSession>();
    let nav = use_navigator();

    use_effect(move || {
        if !session.is_authenticated() {
            nav.push(Route::Auth {});
        }
    });

    rsx! {
        div { class: "home" }
    }
}
