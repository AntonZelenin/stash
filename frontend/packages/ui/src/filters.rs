use std::collections::HashMap;
use std::rc::Rc;
use std::time::Duration;

use api::{ApiError, Collection, ItemCounts, ItemKindCounts, ItemSort, Tag};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::collections::{COLLECTIONS_CSS, COLLECTIONS_ENABLED, CollectionChip};
use crate::i18n::api_error_message;
use crate::icons::{
    IconCheck, IconChevronDown, IconChevronRight, IconClose, IconFilter, IconHeart,
    IconHeartFilled, IconSort,
};
use crate::preferences::Preferences;

/// Also holds the checkbox options (`filter-option`) the collection picker
/// shares.
pub(crate) const FILTERS_CSS: Asset = asset!("/assets/styling/filters.css");
/// Tag chips, shared with the other tag UI (see tags.css).
const TAGS_CSS: Asset = asset!("/assets/styling/tags.css");

/// How long typing in a tag or collection search box must pause before
/// they're fetched.
pub(crate) const TAG_SEARCH_DEBOUNCE: Duration = Duration::from_millis(150);
/// Selected collections and tags shown as chips in the closed Filters
/// control before the rest collapse into "+N".
const MAX_VISIBLE_FILTER_CHIPS: usize = 2;
/// How many of the user's tags or collections a list asks for: the Filters
/// sections, the collection picker and the list under an item's "Add tag"
/// input. All of them scroll, and searching finds the rest.
pub(crate) const TAG_LIST_LIMIT: u32 = 50;

/// What an image or file item holds (the API's `kind`): the options of the
/// Media and Files dropdowns.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ItemKind {
    Image,
    Video,
    Audio,
    Document,
    Book,
    Other,
}

impl ItemKind {
    fn label(self) -> String {
        match self {
            ItemKind::Image => t!("nav-images"),
            ItemKind::Video => t!("nav-video"),
            ItemKind::Audio => t!("nav-audio"),
            ItemKind::Document => t!("nav-documents"),
            ItemKind::Book => t!("nav-books"),
            ItemKind::Other => t!("nav-other-files"),
        }
    }

    /// The dropdown it's in. Kinds have no colour of their own: only their
    /// group has one.
    pub const fn group(self) -> KindGroup {
        match self {
            ItemKind::Image | ItemKind::Video | ItemKind::Audio => KindGroup::Media,
            ItemKind::Document | ItemKind::Book | ItemKind::Other => KindGroup::Files,
        }
    }

    fn count(self, counts: &ItemKindCounts) -> u32 {
        match self {
            ItemKind::Image => counts.image,
            ItemKind::Video => counts.video,
            ItemKind::Audio => counts.audio,
            ItemKind::Document => counts.document,
            ItemKind::Book => counts.book,
            ItemKind::Other => counts.other,
        }
    }

    pub fn api_value(self) -> &'static str {
        match self {
            ItemKind::Image => "image",
            ItemKind::Video => "video",
            ItemKind::Audio => "audio",
            ItemKind::Document => "document",
            ItemKind::Book => "book",
            ItemKind::Other => "other",
        }
    }
}

/// A type tab that opens a dropdown: "All Media"/"All Files", then the
/// group's kinds. The tab itself is not a filter.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum KindGroup {
    Media,
    Files,
}

impl KindGroup {
    pub const fn kinds(self) -> [ItemKind; 3] {
        match self {
            KindGroup::Media => [ItemKind::Image, ItemKind::Video, ItemKind::Audio],
            KindGroup::Files => [ItemKind::Document, ItemKind::Book, ItemKind::Other],
        }
    }

    /// The dropdown's options, in order: the whole group, then each kind.
    pub const fn options(self) -> [TypeFilter; 4] {
        let [first, second, third] = self.kinds();
        [
            TypeFilter::Group(self),
            TypeFilter::Kind(first),
            TypeFilter::Kind(second),
            TypeFilter::Kind(third),
        ]
    }

    fn label(self) -> String {
        match self {
            KindGroup::Media => t!("nav-media"),
            KindGroup::Files => t!("nav-files"),
        }
    }

    fn color_class(self) -> &'static str {
        match self {
            KindGroup::Media => "type-tab-media",
            KindGroup::Files => "type-tab-files",
        }
    }

    /// Whether `filter` is one of this group's options: the tab is then
    /// shown selected.
    pub fn contains(self, filter: TypeFilter) -> bool {
        self.options().contains(&filter)
    }

    fn total(self, counts: &ItemKindCounts) -> u32 {
        self.kinds().iter().map(|kind| kind.count(counts)).sum()
    }

    /// The tab's count: the picked option's, else the whole group's.
    fn count(self, filter: TypeFilter, counts: &ItemCounts) -> u32 {
        if self.contains(filter) {
            filter.count(counts)
        } else {
            self.total(&counts.kinds)
        }
    }
}

/// The item-type filter. Applied by the server, to listing and search alike.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TypeFilter {
    All,
    Text,
    Links,
    /// Every kind of a group: "All Media" or "All Files".
    Group(KindGroup),
    /// One kind of image or file, picked from the Media or Files dropdown.
    Kind(ItemKind),
}

impl TypeFilter {
    fn label(self) -> String {
        match self {
            TypeFilter::All => t!("nav-all-items"),
            TypeFilter::Text => t!("nav-text-notes"),
            TypeFilter::Links => t!("nav-links"),
            TypeFilter::Group(KindGroup::Media) => t!("nav-all-media"),
            TypeFilter::Group(KindGroup::Files) => t!("nav-all-files"),
            TypeFilter::Kind(kind) => kind.label(),
        }
    }

    /// CSS modifier giving each type its accent color (dot, hover tint).
    fn color_class(self) -> &'static str {
        match self {
            TypeFilter::All => "type-tab-all",
            TypeFilter::Text => "type-tab-notes",
            TypeFilter::Links => "type-tab-links",
            TypeFilter::Group(group) => group.color_class(),
            TypeFilter::Kind(kind) => kind.group().color_class(),
        }
    }

    fn count(self, counts: &ItemCounts) -> u32 {
        match self {
            TypeFilter::All => counts.types.total(),
            TypeFilter::Text => counts.types.text,
            TypeFilter::Links => counts.types.link,
            TypeFilter::Group(group) => group.total(&counts.kinds),
            TypeFilter::Kind(kind) => kind.count(&counts.kinds),
        }
    }

    /// The API's `type` value; None for no type filter. "Notes" is this
    /// UI's name for plain `text` items.
    pub fn api_type(self) -> Option<&'static str> {
        match self {
            TypeFilter::Text => Some("text"),
            TypeFilter::Links => Some("link"),
            TypeFilter::All | TypeFilter::Group(_) | TypeFilter::Kind(_) => None,
        }
    }

    /// The API's `kind` values (items of any of them); empty for no kind
    /// filter. A whole group sends each of its kinds.
    pub fn api_kinds(self) -> Vec<&'static str> {
        match self {
            TypeFilter::Kind(kind) => vec![kind.api_value()],
            TypeFilter::Group(group) => group.kinds().map(ItemKind::api_value).to_vec(),
            TypeFilter::All | TypeFilter::Text | TypeFilter::Links => Vec::new(),
        }
    }
}

/// One control of the type tabs, in bar order.
#[derive(Clone, Copy, PartialEq, Debug)]
enum TypeTab {
    Filter(TypeFilter),
    Menu(KindGroup),
}

const TYPE_TABS: [TypeTab; 5] = [
    TypeTab::Filter(TypeFilter::All),
    TypeTab::Filter(TypeFilter::Text),
    TypeTab::Menu(KindGroup::Media),
    TypeTab::Filter(TypeFilter::Links),
    TypeTab::Menu(KindGroup::Files),
];

/// `[ All Items 84 ] • Notes 28  • Media 34 ▾  • Links 9  • Files 13 ▾`:
/// one segment per item type, each with its count (left out until `counts`
/// has loaded); the selected one is filled. Media and Files open a
/// dropdown ("All Media", then each kind), and stay filled while one of
/// its options is picked.
#[component]
pub fn TypeTabs(value: Signal<TypeFilter>, counts: Option<ItemCounts>) -> Element {
    let mut value = value;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        for tab in TYPE_TABS {
            match tab {
                TypeTab::Filter(option) => rsx! {
                    button {
                        class: if value() == option { "type-tab {option.color_class()} type-tab-selected" } else { "type-tab {option.color_class()}" },
                        r#type: "button",
                        aria_pressed: if value() == option { "true" } else { "false" },
                        onclick: move |_| value.set(option),
                        if option != TypeFilter::All {
                            span { class: "type-tab-dot" }
                        }
                        span { "{option.label()}" }
                        if let Some(counts) = &counts {
                            span { class: "filter-count", "{option.count(counts)}" }
                        }
                    }
                },
                TypeTab::Menu(group) => rsx! {
                    KindMenu { group, value, counts }
                },
            }
        }
    }
}

/// The Media or Files tab and its dropdown. Clicking the tab opens or
/// closes the list ("All Media", then each kind); picking an option
/// filters by it and closes it, as does clicking outside, scrolling, or
/// Escape.
///
/// The tabs scroll sideways on narrow screens, which would clip a panel
/// hung below the tab, so the open panel is fixed to the viewport, at the
/// tab's bottom-left corner as measured when it opened.
#[component]
fn KindMenu(group: KindGroup, value: Signal<TypeFilter>, counts: Option<ItemCounts>) -> Element {
    let mut value = value;
    let mut open = use_signal(|| false);
    let mut trigger = use_signal(|| None::<Rc<MountedData>>);
    // The tab's bottom-left corner in viewport pixels; None if it couldn't
    // be measured (the panel then sits in the top-left corner).
    let mut anchor = use_signal(|| None::<(f64, f64)>);

    let current = value();
    let selected = group.contains(current).then_some(current);
    // A single kind is named on the tab: "Media · Video".
    let picked_kind = match selected {
        Some(TypeFilter::Kind(kind)) => Some(kind),
        _ => None,
    };
    let mut class = format!("type-tab {}", group.color_class());
    if selected.is_some() {
        class.push_str(" type-tab-selected");
    }
    if open() {
        class.push_str(" type-tab-open");
    }
    let panel_style = match anchor() {
        Some((x, y)) => format!("--anchor-x: {x}px; --anchor-y: {y}px;"),
        None => String::new(),
    };
    let title = match picked_kind {
        Some(kind) => format!("{}: {}", group.label(), kind.label()),
        None => group.label(),
    };
    // Focused when the panel opens, so Escape and Tab work from there.
    let focused = selected.unwrap_or(TypeFilter::Group(group));

    rsx! {
        div { class: "filter-control kind-menu",
            button {
                class,
                r#type: "button",
                title: "{title}",
                aria_haspopup: "menu",
                aria_expanded: if open() { "true" } else { "false" },
                onmounted: move |evt| trigger.set(Some(evt.data())),
                onclick: move |_| async move {
                    if open() {
                        open.set(false);
                        return;
                    }
                    let rect = match trigger() {
                        Some(trigger) => trigger.get_client_rect().await.ok(),
                        None => None,
                    };
                    anchor.set(rect.map(|rect| (rect.min_x(), rect.max_y())));
                    open.set(true);
                },
                span { class: "type-tab-dot" }
                span { "{group.label()}" }
                if let Some(kind) = picked_kind {
                    span { class: "type-tab-subtype", "{kind.label()}" }
                }
                if let Some(counts) = &counts {
                    span { class: "filter-count", "{group.count(current, counts)}" }
                }
                span { class: "filter-chevron", IconChevronDown {} }
            }
            if open() {
                div {
                    class: "filter-backdrop",
                    onclick: move |_| open.set(false),
                    onwheel: move |_| open.set(false),
                    ontouchmove: move |_| open.set(false),
                }
                div {
                    class: "filter-panel kind-panel",
                    role: "menu",
                    aria_label: "{group.label()}",
                    style: "{panel_style}",
                    onkeydown: move |evt| {
                        if evt.key() == Key::Escape {
                            open.set(false);
                        }
                    },
                    for (index, option) in group.options().into_iter().enumerate() {
                        // "All Media" apart from the single kinds.
                        if index == 1 {
                            div { class: "kind-panel-divider", role: "separator" }
                        }
                        button {
                            // No dot or colour: the label says what it is.
                            class: if selected == Some(option) { "kind-option kind-option-selected" } else { "kind-option" },
                            r#type: "button",
                            role: "menuitemradio",
                            aria_checked: if selected == Some(option) { "true" } else { "false" },
                            onmounted: move |evt| async move {
                                if option == focused {
                                    let _ = evt.set_focus(true).await;
                                }
                            },
                            onclick: move |_| {
                                value.set(option);
                                open.set(false);
                            },
                            span { class: "kind-option-label", "{option.label()}" }
                            if let Some(counts) = &counts {
                                span { class: "filter-count", "{option.count(counts)}" }
                            }
                        }
                    }
                }
            }
        }
    }
}

/// `[ ♥ Favorites 12 ]`: on/off toggle for showing only favorites. The
/// count is left out until it has loaded.
#[component]
pub fn FavoritesToggle(value: Signal<bool>, count: Option<u32>) -> Element {
    let mut value = value;

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        button {
            class: if value() { "favorites-toggle favorites-toggle-active" } else { "favorites-toggle" },
            r#type: "button",
            aria_pressed: if value() { "true" } else { "false" },
            title: if value() { t!("nav-favorites-only-on") } else { t!("nav-favorites-only-off") },
            onclick: move |_| value.toggle(),
            if value() {
                IconHeartFilled {}
            } else {
                IconHeart {}
            }
            span { {t!("nav-favorites")} }
            if let Some(count) = count {
                span { class: "favorites-count", "{count}" }
            }
        }
    }
}

/// Which of the user's things a `FiltersMenu` section (or another
/// checkbox list, such as the hidden tags in Settings) lists.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum FilterSection {
    Collections,
    Tags,
}

impl FilterSection {
    fn title(self) -> String {
        match self {
            FilterSection::Collections => t!("collections-label"),
            FilterSection::Tags => t!("tags-label"),
        }
    }

    fn search_placeholder(self) -> String {
        match self {
            FilterSection::Collections => t!("collections-search-placeholder"),
            FilterSection::Tags => t!("tags-search-placeholder"),
        }
    }

    fn none_yet(self) -> String {
        match self {
            FilterSection::Collections => t!("collections-none-yet"),
            FilterSection::Tags => t!("tags-none-yet"),
        }
    }

    fn no_matches(self) -> String {
        match self {
            FilterSection::Collections => t!("collections-no-matches"),
            FilterSection::Tags => t!("tags-no-matches"),
        }
    }

    fn load_failed(self, error: String) -> String {
        match self {
            FilterSection::Collections => t!("collections-load-failed", error: error),
            FilterSection::Tags => t!("tags-load-failed", error: error),
        }
    }

    /// The user's collections or tags containing `query`, as options. With
    /// `counts_excluding`, tags come with how many items carry them, less
    /// those carrying any of the tags it lists.
    async fn fetch(
        self,
        session: &AuthSession,
        query: String,
        counts_excluding: Option<Vec<String>>,
    ) -> Result<Vec<(Tag, Option<u32>)>, ApiError> {
        match (self, counts_excluding) {
            (FilterSection::Tags, Some(excluded)) => Ok(session
                .list_counted_tags(query, TAG_LIST_LIMIT, excluded)
                .await?
                .into_iter()
                .map(|listed| (listed.tag, Some(listed.item_count)))
                .collect()),
            (FilterSection::Tags, None) => Ok(session
                .list_tags(query, TAG_LIST_LIMIT)
                .await?
                .into_iter()
                .map(|tag| (tag, None))
                .collect()),
            // Same shape as a tag; the section only needs ids and names.
            (FilterSection::Collections, _) => Ok(session
                .list_collections(query, TAG_LIST_LIMIT)
                .await?
                .into_iter()
                .map(|collection| {
                    let option = Tag {
                        id: collection.id,
                        name: collection.name,
                    };
                    (option, None)
                })
                .collect()),
        }
    }
}

/// `[ ≡ Filters ▾ ]`: the collection and tag filters. Closed, it shows the
/// selection as removable chips (collections, then tags: the first few,
/// then "+N"), highlighted like the other active filters. Open, a panel of
/// two collapsible sections, Collections and Tags, each with its own search
/// box and a checkbox per match; any number of either can be selected.
#[component]
pub fn FiltersMenu(tags: Signal<Vec<Tag>>, collections: Signal<Vec<Collection>>) -> Element {
    let mut open = use_signal(|| false);
    let mut tags = tags;
    let mut collections = collections;

    let chosen_collections = collections();
    let chosen_tags = tags();
    let total = chosen_collections.len() + chosen_tags.len();
    let hidden = total.saturating_sub(MAX_VISIBLE_FILTER_CHIPS);
    let visible_collections: Vec<Collection> = chosen_collections
        .iter()
        .take(MAX_VISIBLE_FILTER_CHIPS)
        .cloned()
        .collect();
    let visible_tags: Vec<Tag> = chosen_tags
        .iter()
        .take(MAX_VISIBLE_FILTER_CHIPS - visible_collections.len())
        .cloned()
        .collect();

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }
        document::Link { rel: "stylesheet", href: TAGS_CSS }
        document::Link { rel: "stylesheet", href: COLLECTIONS_CSS }

        div { class: "filter-control filters-menu",
            // A div rather than a button: it contains the chips' own ×
            // buttons, and buttons can't nest.
            div {
                class: if total == 0 { "filter-button" } else { "filter-button filter-button-active" },
                role: "button",
                tabindex: "0",
                title: t!("filters-label"),
                aria_haspopup: "dialog",
                aria_expanded: if open() { "true" } else { "false" },
                onclick: move |_| open.toggle(),
                onkeydown: move |evt| {
                    if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                        evt.prevent_default();
                        open.toggle();
                    }
                },
                IconFilter {}
                if total == 0 {
                    span { class: "filter-button-label", {t!("filters-label")} }
                } else {
                    span { class: "filters-chips",
                        for collection in visible_collections {
                            CollectionChip {
                                key: "c-{collection.id}",
                                name: collection.name.clone(),
                                on_remove: move |_| collections.write().retain(|c| c.id != collection.id),
                            }
                        }
                        for tag in visible_tags {
                            span { class: "tag-chip", key: "t-{tag.id}", title: "{tag.name}",
                                span { class: "tag-chip-name", "{tag.name}" }
                                button {
                                    class: "tag-chip-remove",
                                    r#type: "button",
                                    title: t!("tags-remove-filter"),
                                    aria_label: t!("tags-remove-filter-named", name: tag.name.as_str()),
                                    onclick: move |evt| {
                                        // Don't also toggle the dropdown.
                                        evt.stop_propagation();
                                        tags.write().retain(|t| t.id != tag.id);
                                    },
                                    IconClose {}
                                }
                            }
                        }
                        if hidden > 0 {
                            span { class: "filters-more", "+{hidden}" }
                        }
                    }
                }
                span { class: "filter-chevron", IconChevronDown {} }
            }
            if open() {
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                div {
                    class: "filter-panel filters-panel",
                    role: "dialog",
                    aria_label: t!("filters-label"),
                    onkeydown: move |evt| {
                        if evt.key() == Key::Escape {
                            open.set(false);
                        }
                    },
                    // Sections with a selection start expanded; with none
                    // at all, Tags does, as the old Tags dropdown did.
                    if COLLECTIONS_ENABLED {
                        FilterSectionPanel {
                            section: FilterSection::Collections,
                            initially_expanded: !chosen_collections.is_empty(),
                            selected: chosen_collections
                                .iter()
                                .map(|c| Tag { id: c.id.clone(), name: c.name.clone() })
                                .collect::<Vec<_>>(),
                            on_toggle: move |option: Tag| {
                                let already = collections.read().iter().any(|c| c.id == option.id);
                                if already {
                                    collections.write().retain(|c| c.id != option.id);
                                } else {
                                    collections.write().push(Collection { id: option.id, name: option.name });
                                }
                            },
                        }
                    }
                    FilterSectionPanel {
                        section: FilterSection::Tags,
                        initially_expanded: !chosen_tags.is_empty() || chosen_collections.is_empty(),
                        selected: chosen_tags.clone(),
                        on_toggle: move |tag: Tag| {
                            let already = tags.read().iter().any(|t| t.id == tag.id);
                            if already {
                                tags.write().retain(|t| t.id != tag.id);
                            } else {
                                tags.write().push(tag);
                            }
                        },
                    }
                }
            }
        }
    }
}

/// One collapsible section of the open `FiltersMenu`: a header with a
/// chevron (right when collapsed, down when expanded) and how many are
/// selected; expanded, a search box and the matching options as checkboxes
/// (with nothing typed, the ones selected when it expanded first). `on_toggle` gets an
/// option (de)selected. Fetches as it expands and as the search changes.
#[component]
fn FilterSectionPanel(
    section: FilterSection,
    initially_expanded: bool,
    selected: Vec<Tag>,
    on_toggle: EventHandler<Tag>,
) -> Element {
    let mut expanded = use_signal(|| initially_expanded);
    let selected_count = selected.len();

    rsx! {
        div { class: if expanded() { "filters-section filters-section-expanded" } else { "filters-section" },
            button {
                class: "filters-section-header",
                r#type: "button",
                aria_expanded: if expanded() { "true" } else { "false" },
                onclick: move |_| expanded.toggle(),
                span { class: "filters-section-chevron", IconChevronRight {} }
                span { class: "filters-section-title", "{section.title()}" }
                if selected_count > 0 {
                    span { class: "filter-count filters-section-count", "{selected_count}" }
                }
            }
            if expanded() {
                FilterSectionOptions {
                    section,
                    selected: selected.iter().map(|option| option.id.clone()).collect::<Vec<_>>(),
                    selected_options: selected,
                    show_counts: true,
                    on_toggle,
                }
            }
        }
    }
}

/// A search box and the matching options as checkboxes: the expanded
/// part of a `FilterSectionPanel`, and the hidden tags in Settings. Fetches
/// the current list as it mounts and as the search changes. `selected`: the
/// ids of the checked options; `selected_options`: those of them whose names
/// are known, so they can be listed even if the first page of options
/// doesn't have them. `on_toggle` gets an option (un)checked.
///
/// With `show_counts`, each tag fetched shows how many items carry it, "books
/// (13)": those the list would show, so in Blind mode less the ones with a
/// hidden tag.
#[component]
pub(crate) fn FilterSectionOptions(
    section: FilterSection,
    selected: Vec<String>,
    #[props(default)] selected_options: Vec<Tag>,
    #[props(default)] show_counts: bool,
    on_toggle: EventHandler<Tag>,
) -> Element {
    let session = use_context::<AuthSession>();
    let preferences = use_context::<Preferences>();
    let mut query = use_signal(String::new);
    // What was selected when it mounted, listed first: the ids, and the
    // options known by name.
    let (pinned_ids, pinned) = use_hook({
        let selected = selected.clone();
        let selected_options = selected_options.clone();
        move || (selected, selected_options)
    });

    // Re-runs as the query changes; the delay debounces typing (a newer
    // query drops the pending fetch).
    let matches = use_resource(move || {
        let session = session.clone();
        let query = query().trim().to_string();
        // Read here, so switching modes refetches the counts.
        let counts_excluding = show_counts.then(|| preferences.excluded_tag_ids());
        async move {
            if !query.is_empty() {
                Delay::new(TAG_SEARCH_DEBOUNCE).await;
            }
            section.fetch(&session, query, counts_excluding).await
        }
    });

    let searching = !query().trim().is_empty();
    let is_selected = |option: &Tag| selected.contains(&option.id);

    rsx! {
        input {
            class: "filters-search",
            r#type: "search",
            placeholder: section.search_placeholder(),
            value: "{query}",
            oninput: move |evt| query.set(evt.value()),
            onmounted: move |evt| async move {
                let _ = evt.set_focus(true).await;
            },
        }
        div { class: "filters-options",
            match &*matches.read() {
                Some(Ok(found)) => {
                    // Counts only for those fetched: an option pinned
                    // without being on this page shows none.
                    let counts: HashMap<String, u32> = found
                        .iter()
                        .filter_map(|(option, count)| Some((option.id.clone(), (*count)?)))
                        .collect();
                    let found: Vec<Tag> = found.iter().map(|(option, _)| option.clone()).collect();
                    // While browsing, the ones selected when it mounted
                    // first (those known by name even if not fetched), so
                    // they stay in reach however many there are, and
                    // selecting more doesn't move anything under the
                    // pointer; just matches while searching.
                    let options: Vec<Tag> = if searching {
                        found.clone()
                    } else {
                        let is_known = |option: &Tag| pinned.iter().any(|p| p.id == option.id);
                        let was_selected = |option: &&Tag| pinned_ids.contains(&option.id);
                        let (found_pinned, rest): (Vec<&Tag>, Vec<&Tag>) =
                            found.iter().filter(|option| !is_known(option)).partition(was_selected);
                        pinned
                            .iter()
                            .cloned()
                            .chain(found_pinned.into_iter().cloned())
                            .chain(rest.into_iter().cloned())
                            .collect()
                    };
                    let truncated = found.len() >= TAG_LIST_LIMIT as usize;
                    if options.is_empty() {
                        rsx! {
                            p { class: "filter-empty",
                                if searching { "{section.no_matches()}" } else { "{section.none_yet()}" }
                            }
                        }
                    } else {
                        rsx! {
                            for option in options {
                                label { class: "filter-option", key: "{option.id}",
                                    input {
                                        r#type: "checkbox",
                                        checked: is_selected(&option),
                                        onchange: {
                                            let option = option.clone();
                                            move |_| on_toggle.call(option.clone())
                                        },
                                    }
                                    span { "{option.name}" }
                                    if let Some(count) = counts.get(&option.id) {
                                        span { class: "filter-option-count", "({count})" }
                                    }
                                }
                            }
                            // Only the first page is listed: searching finds
                            // the rest.
                            if truncated {
                                p { class: "filter-empty filters-more-hint", {t!("filters-refine-search")} }
                            }
                        }
                    }
                }
                Some(Err(err)) => rsx! {
                    p { class: "filter-empty", "{section.load_failed(api_error_message(err))}" }
                },
                None => rsx! {
                    p { class: "filter-empty", {t!("common-loading")} }
                },
            }
        }
    }
}

/// The sort orders offered, in menu order.
const SORT_OPTIONS: [ItemSort; 3] = [ItemSort::Newest, ItemSort::Oldest, ItemSort::Random];

fn sort_label(sort: ItemSort) -> String {
    match sort {
        ItemSort::Newest => t!("sort-newest"),
        ItemSort::Oldest => t!("sort-oldest"),
        ItemSort::Random => t!("sort-random"),
    }
}

/// `[ ⇅ ]`: listing order, at the right end of the filter bar. Highlighted
/// when not the default (newest first). Picking Random again reshuffles
/// (`on_reshuffle`). Disabled while searching, since search results are
/// ranked by relevance.
#[component]
pub fn SortMenu(
    value: Signal<ItemSort>,
    on_reshuffle: EventHandler<()>,
    disabled: bool,
) -> Element {
    let mut open = use_signal(|| false);
    let mut value = value;
    let analytics = crate::analytics::use_analytics();

    let current = value();
    let title = if disabled {
        t!("sort-search-relevance")
    } else {
        t!("sort-title", order: sort_label(current))
    };
    let mut class = String::from("filter-button sort-button");
    if current != ItemSort::default() {
        class.push_str(" filter-button-active");
    }

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        div { class: "filter-control sort-menu",
            button {
                class,
                r#type: "button",
                title: "{title}",
                aria_label: "{title}",
                aria_haspopup: "menu",
                aria_expanded: if open() { "true" } else { "false" },
                disabled,
                onclick: move |_| open.toggle(),
                IconSort {}
            }
            if open() && !disabled {
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                div { class: "filter-panel sort-panel", role: "menu",
                    for option in SORT_OPTIONS {
                        button {
                            class: if option == current { "sort-option sort-option-selected" } else { "sort-option" },
                            r#type: "button",
                            role: "menuitemradio",
                            aria_checked: if option == current { "true" } else { "false" },
                            title: if option == ItemSort::Random && current == ItemSort::Random { t!("sort-reshuffle") } else { String::new() },
                            onclick: {
                                let analytics = analytics.clone();
                                move |_| {
                                    if option == ItemSort::Random && value() == ItemSort::Random {
                                        on_reshuffle.call(());
                                    } else if option != value() {
                                        value.set(option);
                                        analytics.sort_changed(option);
                                    }
                                    open.set(false);
                                }
                            },
                            span { class: "sort-option-check",
                                if option == current {
                                    IconCheck {}
                                }
                            }
                            span { "{sort_label(option)}" }
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use api::{ItemKindCounts, ItemTypeCounts};

    use super::*;
    use crate::i18n::Language;
    use crate::i18n::tests::in_language;

    const ALL_KINDS: [ItemKind; 6] = [
        ItemKind::Image,
        ItemKind::Video,
        ItemKind::Audio,
        ItemKind::Document,
        ItemKind::Book,
        ItemKind::Other,
    ];

    fn counts() -> ItemCounts {
        ItemCounts {
            types: ItemTypeCounts {
                text: 5,
                link: 4,
                image: 3,
                file: 27,
            },
            kinds: ItemKindCounts {
                image: 3,
                video: 2,
                audio: 1,
                document: 10,
                book: 6,
                other: 8,
            },
            favorites: 7,
        }
    }

    #[test]
    fn top_level_filters_send_a_type_and_kinds_send_kinds() {
        let api = |filter: TypeFilter| (filter.api_type(), filter.api_kinds());
        assert_eq!(api(TypeFilter::Text), (Some("text"), vec![]));
        assert_eq!(api(TypeFilter::Links), (Some("link"), vec![]));
        assert_eq!(
            api(TypeFilter::Kind(ItemKind::Video)),
            (None, vec!["video"])
        );
        assert_eq!(api(TypeFilter::Kind(ItemKind::Book)), (None, vec!["book"]));
        let values: Vec<_> = ALL_KINDS.iter().map(|kind| kind.api_value()).collect();
        assert_eq!(
            values,
            ["image", "video", "audio", "document", "book", "other"]
        );
    }

    #[test]
    fn all_media_and_all_files_send_every_kind_of_their_group() {
        assert_eq!(
            TypeFilter::Group(KindGroup::Media).api_kinds(),
            ["image", "video", "audio"]
        );
        assert_eq!(
            TypeFilter::Group(KindGroup::Files).api_kinds(),
            ["document", "book", "other"]
        );
        for group in [KindGroup::Media, KindGroup::Files] {
            assert_eq!(TypeFilter::Group(group).api_type(), None);
        }
    }

    #[test]
    fn all_items_clears_the_type_and_kind() {
        let mut filter = TypeFilter::Kind(ItemKind::Audio);
        assert!(KindGroup::Media.contains(filter));

        filter = TypeFilter::All;

        assert_eq!((filter.api_type(), filter.api_kinds()), (None, vec![]));
        assert!(!KindGroup::Media.contains(filter));
        assert!(!KindGroup::Files.contains(filter));
    }

    #[test]
    fn each_dropdown_starts_with_its_all_option_then_its_kinds() {
        assert_eq!(
            KindGroup::Media.options(),
            [
                TypeFilter::Group(KindGroup::Media),
                TypeFilter::Kind(ItemKind::Image),
                TypeFilter::Kind(ItemKind::Video),
                TypeFilter::Kind(ItemKind::Audio),
            ]
        );
        assert_eq!(
            KindGroup::Files.options(),
            [
                TypeFilter::Group(KindGroup::Files),
                TypeFilter::Kind(ItemKind::Document),
                TypeFilter::Kind(ItemKind::Book),
                TypeFilter::Kind(ItemKind::Other),
            ]
        );
        for kind in ALL_KINDS {
            let groups = [KindGroup::Media, KindGroup::Files]
                .into_iter()
                .filter(|group| group.kinds().contains(&kind))
                .count();
            assert_eq!(groups, 1, "{kind:?}");
        }
    }

    #[test]
    fn a_picked_option_selects_only_its_own_group() {
        for option in KindGroup::Media.options() {
            assert!(KindGroup::Media.contains(option), "{option:?}");
            assert!(!KindGroup::Files.contains(option), "{option:?}");
        }
        for option in KindGroup::Files.options() {
            assert!(KindGroup::Files.contains(option), "{option:?}");
            assert!(!KindGroup::Media.contains(option), "{option:?}");
        }
        // Top-level filters select neither group.
        for filter in [TypeFilter::All, TypeFilter::Text, TypeFilter::Links] {
            assert!(!KindGroup::Media.contains(filter));
            assert!(!KindGroup::Files.contains(filter));
        }
    }

    #[test]
    fn kinds_have_no_colour_of_their_own() {
        for kind in ALL_KINDS {
            assert!(kind.group().kinds().contains(&kind), "{kind:?}");
            assert_eq!(
                TypeFilter::Kind(kind).color_class(),
                kind.group().color_class()
            );
        }
        assert_eq!(KindGroup::Media.color_class(), "type-tab-media");
        assert_eq!(KindGroup::Files.color_class(), "type-tab-files");
    }

    #[test]
    fn media_and_files_tabs_are_menus_not_filters() {
        assert_eq!(
            TYPE_TABS,
            [
                TypeTab::Filter(TypeFilter::All),
                TypeTab::Filter(TypeFilter::Text),
                TypeTab::Menu(KindGroup::Media),
                TypeTab::Filter(TypeFilter::Links),
                TypeTab::Menu(KindGroup::Files),
            ]
        );
    }

    #[test]
    fn top_level_counts_are_unchanged() {
        let counts = counts();
        assert_eq!(TypeFilter::All.count(&counts), 5 + 4 + 3 + 27);
        assert_eq!(TypeFilter::Text.count(&counts), 5);
        assert_eq!(TypeFilter::Links.count(&counts), 4);
    }

    #[test]
    fn options_and_tabs_are_counted() {
        let counts = counts();
        let got: Vec<_> = ALL_KINDS
            .iter()
            .map(|kind| TypeFilter::Kind(*kind).count(&counts))
            .collect();
        assert_eq!(got, [3, 2, 1, 10, 6, 8]);
        assert_eq!(
            TypeFilter::Group(KindGroup::Media).count(&counts),
            3 + 2 + 1
        );
        assert_eq!(
            TypeFilter::Group(KindGroup::Files).count(&counts),
            10 + 6 + 8
        );

        // Nothing of the group picked: the whole group.
        assert_eq!(KindGroup::Media.count(TypeFilter::All, &counts), 6);
        assert_eq!(KindGroup::Files.count(TypeFilter::Text, &counts), 24);
        // One of its options picked: what the tab shows.
        let video = TypeFilter::Kind(ItemKind::Video);
        assert_eq!(KindGroup::Media.count(video, &counts), 2);
        assert_eq!(KindGroup::Files.count(video, &counts), 24);
        let all_files = TypeFilter::Group(KindGroup::Files);
        assert_eq!(KindGroup::Files.count(all_files, &counts), 24);
    }

    #[test]
    fn options_are_labelled() {
        in_language(Language::English, || {
            assert_eq!(KindGroup::Media.label(), "Media");
            assert_eq!(KindGroup::Files.label(), "Files");
            let media: Vec<_> = KindGroup::Media.options().map(TypeFilter::label).to_vec();
            assert_eq!(media, ["All Media", "Images", "Video", "Audio"]);
            let files: Vec<_> = KindGroup::Files.options().map(TypeFilter::label).to_vec();
            assert_eq!(files, ["All Files", "Documents", "Books", "Other"]);
        });
    }
}
