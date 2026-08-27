use dioxus::prelude::*;

use crate::{Auth, Home};

#[derive(Clone, Debug, PartialEq, Routable)]
pub enum Route {
    #[route("/")]
    Home {},

    #[route("/login")]
    Auth {},
}
