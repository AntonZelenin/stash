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
