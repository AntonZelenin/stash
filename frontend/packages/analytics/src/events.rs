//! Every event the clients send, and its properties.
//!
//! Properties are built from enums, booleans and counts only, so free text
//! can't get in: no item content, descriptions, filenames, URLs, search
//! queries, tag names or ids, emails or tokens. `ALLOWED_PROPERTIES` lists
//! what each event may carry, and the tests check every event against it.

use serde_json::{Map, Value, json};

/// Which client sent an event.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Web,
    Desktop,
    Mobile,
}

impl Platform {
    pub fn code(self) -> &'static str {
        match self {
            Platform::Web => "web",
            Platform::Desktop => "desktop",
            Platform::Mobile => "mobile",
        }
    }
}

/// The Normal/Blind display mode.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DisplayMode {
    Normal,
    Blind,
}

impl DisplayMode {
    pub fn code(self) -> &'static str {
        match self {
            DisplayMode::Normal => "normal",
            DisplayMode::Blind => "blind",
        }
    }
}

/// An item's type, as the API names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ItemType {
    Text,
    Link,
    Image,
    File,
}

impl ItemType {
    /// The type for the API's name of it; None for anything else, so an
    /// unknown value is never sent.
    pub fn from_api(value: &str) -> Option<ItemType> {
        match value {
            "text" => Some(ItemType::Text),
            "link" => Some(ItemType::Link),
            "image" => Some(ItemType::Image),
            "file" => Some(ItemType::File),
            _ => None,
        }
    }

    pub fn code(self) -> &'static str {
        match self {
            ItemType::Text => "text",
            ItemType::Link => "link",
            ItemType::Image => "image",
            ItemType::File => "file",
        }
    }
}

/// A listing order: by date (newest or oldest first), or random.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sort {
    NewestFirst,
    OldestFirst,
    Random,
}

/// Which filter categories are active (the date and collection filters
/// aren't tracked).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FilterCategories {
    pub file_type: bool,
    pub tags: bool,
    pub favourites: bool,
    /// How many tags are selected (never which).
    pub selected_tag_count: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Event {
    /// Opening the app, or the first activity after `SESSION_IDLE_TIMEOUT`
    /// without any.
    SessionStarted {
        platform: Platform,
        mode: DisplayMode,
    },
    ModeChanged {
        mode: DisplayMode,
    },
    ItemOpened {
        item_type: ItemType,
        from_search: bool,
        mode: DisplayMode,
    },
    SortChanged {
        sort: Sort,
    },
    FiltersChanged {
        filters: FilterCategories,
    },
    AccountSettingsOpened,
    PrivacyOpened,
}

/// Each event's name and the only properties it may carry (checked by
/// the tests against every event).
#[cfg(test)]
pub(crate) const ALLOWED_PROPERTIES: [(&str, &[&str]); 7] = [
    ("session_started", &["platform", "mode"]),
    ("mode_changed", &["mode"]),
    ("item_opened", &["item_type", "from_search", "mode"]),
    ("sort_changed", &["sort_method", "sort_direction"]),
    (
        "filters_changed",
        &[
            "file_type_filter",
            "tag_filter",
            "favourites_filter",
            "active_filter_count",
            "selected_tag_count",
        ],
    ),
    ("account_settings_opened", &[]),
    ("privacy_opened", &[]),
];

impl Event {
    pub fn name(&self) -> &'static str {
        match self {
            Event::SessionStarted { .. } => "session_started",
            Event::ModeChanged { .. } => "mode_changed",
            Event::ItemOpened { .. } => "item_opened",
            Event::SortChanged { .. } => "sort_changed",
            Event::FiltersChanged { .. } => "filters_changed",
            Event::AccountSettingsOpened => "account_settings_opened",
            Event::PrivacyOpened => "privacy_opened",
        }
    }

    /// The event's own properties.
    pub fn properties(&self) -> Map<String, Value> {
        let value = match self {
            Event::SessionStarted { platform, mode } => json!({
                "platform": platform.code(),
                "mode": mode.code(),
            }),
            Event::ModeChanged { mode } => json!({ "mode": mode.code() }),
            Event::ItemOpened {
                item_type,
                from_search,
                mode,
            } => json!({
                "item_type": item_type.code(),
                "from_search": from_search,
                "mode": mode.code(),
            }),
            Event::SortChanged { sort } => match sort {
                Sort::NewestFirst => json!({ "sort_method": "date", "sort_direction": "desc" }),
                Sort::OldestFirst => json!({ "sort_method": "date", "sort_direction": "asc" }),
                Sort::Random => json!({ "sort_method": "random" }),
            },
            Event::FiltersChanged { filters } => json!({
                "file_type_filter": filters.file_type,
                "tag_filter": filters.tags,
                "favourites_filter": filters.favourites,
                "active_filter_count":
                    u32::from(filters.file_type) + u32::from(filters.tags) + u32::from(filters.favourites),
                "selected_tag_count": filters.selected_tag_count,
            }),
            Event::AccountSettingsOpened | Event::PrivacyOpened => json!({}),
        };
        match value {
            Value::Object(map) => map,
            _ => Map::new(),
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn every_event() -> Vec<Event> {
        vec![
            Event::SessionStarted {
                platform: Platform::Web,
                mode: DisplayMode::Normal,
            },
            Event::ModeChanged {
                mode: DisplayMode::Blind,
            },
            Event::ItemOpened {
                item_type: ItemType::Image,
                from_search: true,
                mode: DisplayMode::Blind,
            },
            Event::SortChanged {
                sort: Sort::NewestFirst,
            },
            Event::SortChanged {
                sort: Sort::OldestFirst,
            },
            Event::SortChanged { sort: Sort::Random },
            Event::FiltersChanged {
                filters: FilterCategories {
                    file_type: true,
                    tags: true,
                    favourites: false,
                    selected_tag_count: 2,
                },
            },
            Event::AccountSettingsOpened,
            Event::PrivacyOpened,
        ]
    }

    pub(crate) fn allowed(name: &str) -> &'static [&'static str] {
        ALLOWED_PROPERTIES
            .iter()
            .find(|(event, _)| *event == name)
            .map(|(_, properties)| *properties)
            .unwrap_or_else(|| panic!("{name} has no allowlist"))
    }

    #[test]
    fn every_property_is_allowed_and_a_closed_value() {
        for event in every_event() {
            let allowed = allowed(event.name());
            for (name, value) in event.properties() {
                assert!(allowed.contains(&name.as_str()), "{}: {name}", event.name());
                // Strings only from the enums above, otherwise flags and
                // counts: never free text.
                match &value {
                    Value::String(code) => assert!(
                        [
                            "web", "desktop", "mobile", "normal", "blind", "text", "link", "image",
                            "file", "date", "random", "asc", "desc"
                        ]
                        .contains(&code.as_str()),
                        "{}: {name} = {code}",
                        event.name()
                    ),
                    Value::Bool(_) | Value::Number(_) => {}
                    other => panic!("{}: {name} = {other}", event.name()),
                }
            }
        }
    }

    #[test]
    fn every_event_has_an_allowlist() {
        let names: Vec<&str> = every_event().iter().map(Event::name).collect();
        for (name, _) in ALLOWED_PROPERTIES {
            assert!(names.contains(&name), "{name} isn't exercised");
        }
    }

    #[test]
    fn unknown_item_types_are_not_sent() {
        assert_eq!(ItemType::from_api("image"), Some(ItemType::Image));
        assert_eq!(ItemType::from_api("my-secret-file.pdf"), None);
    }

    #[test]
    fn filter_counts() {
        let properties = Event::FiltersChanged {
            filters: FilterCategories {
                file_type: true,
                tags: false,
                favourites: true,
                selected_tag_count: 0,
            },
        }
        .properties();
        assert_eq!(properties["active_filter_count"], 2);
        assert_eq!(properties["selected_tag_count"], 0);
    }
}
