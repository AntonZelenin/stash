use dioxus::prelude::*;

macro_rules! icon {
    ($name:ident, $class:literal, $svg:literal) => {
        #[component]
        pub fn $name() -> Element {
            rsx! {
                span { class: concat!("icon ", $class), dangerous_inner_html: $svg }
            }
        }
    };
}

icon!(
    IconStash,
    "icon-stash",
    r##"<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><defs><linearGradient id="stash-mark-gradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#fca01e"/><stop offset="25%" stop-color="#fd5969"/><stop offset="50%" stop-color="#ef21ea"/><stop offset="75%" stop-color="#6675f5"/><stop offset="100%" stop-color="#1bd4b3"/></linearGradient></defs><path stroke="url(#stash-mark-gradient)" d="M12 6.5 6 9v9h12V9Z"/><path stroke="url(#stash-mark-gradient)" d="M6 9 12 11.5 18 9"/></svg>"##
);

icon!(
    IconMail,
    "icon-mail",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/></svg>"#
);

icon!(
    IconLock,
    "icon-lock",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>"#
);

icon!(
    IconEye,
    "icon-eye",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/></svg>"#
);

icon!(
    IconEyeOff,
    "icon-eye-off",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/></svg>"#
);

icon!(
    IconMenu,
    "icon-menu",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/></svg>"#
);

icon!(
    IconUser,
    "icon-user",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.5-7 8-7s8 3 8 7"/></svg>"#
);

icon!(
    IconSliders,
    "icon-sliders",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h4"/><path d="M14 6h6"/><circle cx="11" cy="6" r="2.2"/><path d="M4 12h9"/><path d="M17 12h3"/><circle cx="14.2" cy="12" r="2.2"/><path d="M4 18h2"/><path d="M11 18h9"/><circle cx="7.8" cy="18" r="2.2"/></svg>"#
);

icon!(
    IconHelp,
    "icon-help",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1.3 1-1.3 1.9v.3"/><path d="M12 17h.01"/></svg>"#
);

icon!(
    IconLogout,
    "icon-logout",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>"#
);

icon!(
    IconArrowUp,
    "icon-arrow-up",
    r#"<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>"#
);
