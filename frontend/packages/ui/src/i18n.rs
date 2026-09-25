//! Translations for the UI, built on `dioxus-i18n` (Fluent).
//!
//! Every user-facing string lives in `i18n/<code>.ftl` and is looked up by
//! key with `t!("key")` (or `t!("key", name: value)` for placeholders).
//! English is always loaded underneath the selected language, so a key a
//! translation lacks falls back to English.
//!
//! The language is chosen once at startup — the user's saved choice if any,
//! otherwise the first supported system/browser language, otherwise English
//! — and can be changed at runtime with `Localization::set_language`,
//! which rerenders everything showing translated text.
//!
//! To add a language: add `i18n/<code>.ftl` with the same keys as `en.ftl`,
//! then a `Language` variant and its entries in the `impl` below.

use std::sync::Arc;

use dioxus::prelude::*;
use dioxus_i18n::prelude::{I18n, I18nConfig, use_init_i18n};
use dioxus_i18n::t;
use dioxus_i18n::unic_langid::LanguageIdentifier;

/// A language the UI is translated into.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Language {
    English,
    Ukrainian,
}

impl Language {
    /// Every supported language, in the order the language picker lists
    /// them.
    pub const ALL: [Language; 2] = [Language::English, Language::Ukrainian];
    /// Used when no preferred language is supported, and for any key a
    /// translation is missing.
    pub const FALLBACK: Language = Language::English;

    /// The language's BCP 47 code: what's saved, and the `lang` attribute.
    pub fn code(self) -> &'static str {
        match self {
            Language::English => "en",
            Language::Ukrainian => "uk",
        }
    }

    /// The language's name in itself, so it can be found in the picker
    /// whatever language the UI is currently in.
    pub fn native_name(self) -> &'static str {
        match self {
            Language::English => "English",
            Language::Ukrainian => "Українська",
        }
    }

    fn translations(self) -> &'static str {
        match self {
            Language::English => include_str!("../i18n/en.ftl"),
            Language::Ukrainian => include_str!("../i18n/uk.ftl"),
        }
    }

    /// Month names and the like for dates.
    pub(crate) fn chrono_locale(self) -> chrono::Locale {
        match self {
            Language::English => chrono::Locale::en_GB,
            Language::Ukrainian => chrono::Locale::uk_UA,
        }
    }

    /// `value` with `decimals` digits after the language's decimal
    /// separator (`1.2` in English, `1,2` in Ukrainian).
    pub(crate) fn format_decimal(self, value: f64, decimals: usize) -> String {
        let formatted = format!("{value:.decimals$}");
        match self {
            Language::English => formatted,
            Language::Ukrainian => formatted.replace('.', ","),
        }
    }

    /// The supported language for a BCP 47 tag such as `"uk-UA"` or
    /// `"en_US"`, matched on its primary language subtag.
    pub fn from_tag(tag: &str) -> Option<Language> {
        let primary = tag.split(['-', '_']).next()?.to_ascii_lowercase();
        Language::ALL
            .into_iter()
            .find(|language| language.code() == primary)
    }

    /// The language to start in: `saved` (the user's own choice) if it's
    /// still supported, otherwise the first supported of `preferred` (the
    /// browser's/system's languages, most preferred first), otherwise the
    /// fallback.
    pub fn negotiate(saved: Option<&str>, preferred: &[String]) -> Language {
        saved
            .and_then(Language::from_tag)
            .or_else(|| preferred.iter().find_map(|tag| Language::from_tag(tag)))
            .unwrap_or(Language::FALLBACK)
    }

    fn langid(self) -> LanguageIdentifier {
        self.code()
            .parse()
            .expect("language codes are valid identifiers")
    }
}

/// Where the user's chosen language is kept between sessions. Implemented
/// by each platform (e.g. `localStorage` on the web).
pub trait LanguageStore {
    /// The saved language code, if the user has chosen one.
    fn load(&self) -> Option<String>;
    fn save(&self, code: &str);
}

/// Shared, reactive language state, provided by `use_init_localization`
/// and read with `use_context::<Localization>()`.
#[derive(Clone)]
pub struct Localization {
    i18n: I18n,
    store: Arc<dyn LanguageStore>,
    language: Signal<Language>,
}

impl PartialEq for Localization {
    fn eq(&self, other: &Self) -> bool {
        self.language == other.language
    }
}

impl Localization {
    pub fn language(&self) -> Language {
        (self.language)()
    }

    /// Switches the UI to `language` and remembers it as the user's
    /// choice, overriding the system language from now on.
    pub fn set_language(&self, language: Language) {
        let mut i18n = self.i18n;
        i18n.set_language(language.langid());
        let mut slot = self.language;
        slot.set(language);
        self.store.save(language.code());
    }
}

/// Sets up translations for the component tree below the caller. Call once,
/// in the app root. `system_languages` are the browser's/OS's preferred
/// languages, most preferred first; the saved choice in `store` wins over
/// them.
pub fn use_init_localization(
    store: Arc<dyn LanguageStore>,
    system_languages: Vec<String>,
) -> Localization {
    let initial = use_hook(|| Language::negotiate(store.load().as_deref(), &system_languages));
    let i18n = use_init_i18n(move || i18n_config(initial));
    let localization = use_context_provider(|| Localization {
        i18n,
        store,
        language: Signal::new(initial),
    });

    // The document's language, for screen readers, spell checking and
    // hyphenation.
    let language = localization.language;
    use_effect(move || {
        document::eval(&format!(
            "document.documentElement.lang = {:?};",
            language().code()
        ));
    });

    localization
}

fn i18n_config(initial: Language) -> I18nConfig {
    Language::ALL.into_iter().fold(
        I18nConfig::new(initial.langid()).with_fallback(Language::FALLBACK.langid()),
        |config, language| config.with_locale((language.langid(), language.translations())),
    )
}

/// The current UI language, for formatting dates and numbers; reading it
/// in a component rerenders that component when it changes. Not a hook, so
/// it can be called anywhere under `use_init_localization`.
pub(crate) fn current_language() -> Language {
    consume_context::<Localization>().language()
}

/// The message to show for an API error. Validation messages come from the
/// backend as they are; the rest are translated here.
pub(crate) fn api_error_message(err: &api::ApiError) -> String {
    use api::ApiError;
    match err {
        ApiError::Network => t!("error-network"),
        ApiError::Unauthorized => t!("error-unauthorized"),
        ApiError::Conflict => t!("error-conflict"),
        ApiError::Validation(errors) if !errors.is_empty() => errors
            .iter()
            .map(|error| error.message.as_str())
            .collect::<Vec<_>>()
            .join(" "),
        ApiError::Validation(_) => t!("error-invalid-request"),
        ApiError::Server => t!("error-server"),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use std::collections::BTreeSet;
    use std::sync::Mutex;

    use super::*;

    struct MemoryStore(Mutex<Option<String>>);

    impl LanguageStore for MemoryStore {
        fn load(&self) -> Option<String> {
            self.0.lock().unwrap().clone()
        }

        fn save(&self, code: &str) {
            *self.0.lock().unwrap() = Some(code.to_string());
        }
    }

    #[derive(Clone, PartialEq, Props)]
    struct RootProps {
        language: Language,
    }

    #[allow(non_snake_case)]
    fn Root(props: RootProps) -> Element {
        let store = Arc::new(MemoryStore(Mutex::new(Some(props.language.code().into()))));
        use_init_localization(store, Vec::new());
        rsx! {}
    }

    /// Runs `f` where `t!` and `current_language` work, in `language`.
    pub(crate) fn in_language<T>(language: Language, f: impl FnOnce() -> T) -> T {
        let mut dom = VirtualDom::new_with_props(Root, RootProps { language });
        dom.rebuild_in_place();
        // `Root` itself, below the scopes Dioxus wraps around the app.
        dom.in_scope(ScopeId::APP, f)
    }

    fn keys(language: Language) -> BTreeSet<String> {
        language
            .translations()
            .lines()
            .filter_map(|line| line.split_once(" ="))
            .map(|(key, _)| key)
            .filter(|key| !key.is_empty() && !key.starts_with([' ', '#', '.']))
            .map(str::to_string)
            .collect()
    }

    #[test]
    fn every_language_has_exactly_the_english_keys() {
        let english = keys(Language::English);
        for language in Language::ALL {
            let other = keys(language);
            let missing: Vec<_> = english.difference(&other).collect();
            let extra: Vec<_> = other.difference(&english).collect();
            assert!(
                missing.is_empty() && extra.is_empty(),
                "{language:?}: missing {missing:?}, unknown {extra:?}"
            );
        }
    }

    #[test]
    fn every_key_used_in_the_code_exists() {
        let english = keys(Language::English);
        let source_dir = concat!(env!("CARGO_MANIFEST_DIR"), "/src");
        for entry in std::fs::read_dir(source_dir).unwrap() {
            let path = entry.unwrap().path();
            let source = std::fs::read_to_string(&path).unwrap();
            let code = source
                .lines()
                .filter(|line| !line.trim_start().starts_with("//"));
            for line in code {
                for (index, _) in line.match_indices("t!(\"") {
                    // Not the end of another macro's name, like `format!("`.
                    let preceding = line[..index].chars().next_back();
                    if preceding.is_some_and(|c| c.is_alphanumeric() || c == '_') {
                        continue;
                    }
                    let rest = &line[index + 4..];
                    let key = &rest[..rest.find('"').unwrap()];
                    assert!(
                        english.contains(key),
                        "{}: unknown key {key:?}",
                        path.display()
                    );
                }
            }
        }
    }

    #[test]
    fn translations_parse_and_format() {
        for language in Language::ALL {
            in_language(language, || {
                // Panics if the file failed to parse or the key is missing.
                assert!(!t!("nav-all-items").is_empty());
                assert!(!t!("auth-password-too-short", min: 8).is_empty());
            });
        }
        assert_eq!(in_language(Language::English, || t!("common-save")), "Save");
        assert_eq!(
            in_language(Language::Ukrainian, || t!("common-save")),
            "Зберегти"
        );
    }

    #[test]
    fn saved_choice_then_system_language_then_english() {
        let preferred = vec!["de-DE".to_string(), "uk-UA".to_string(), "en".to_string()];
        assert_eq!(
            Language::negotiate(Some("en"), &preferred),
            Language::English
        );
        assert_eq!(Language::negotiate(None, &preferred), Language::Ukrainian);
        // A saved language no longer supported is ignored.
        assert_eq!(
            Language::negotiate(Some("xx"), &preferred),
            Language::Ukrainian
        );
        assert_eq!(
            Language::negotiate(None, &["fr".to_string()]),
            Language::English
        );
        assert_eq!(Language::negotiate(None, &[]), Language::English);
    }

    #[test]
    fn tags_match_on_their_primary_language() {
        assert_eq!(Language::from_tag("uk"), Some(Language::Ukrainian));
        assert_eq!(Language::from_tag("UK-ua"), Some(Language::Ukrainian));
        assert_eq!(Language::from_tag("en_US"), Some(Language::English));
        assert_eq!(Language::from_tag("ru"), None);
        assert_eq!(Language::from_tag(""), None);
    }

    #[test]
    fn decimals_use_the_languages_separator() {
        assert_eq!(Language::English.format_decimal(1.26, 1), "1.3");
        assert_eq!(Language::Ukrainian.format_decimal(1.26, 1), "1,3");
    }
}
