//! Selecting several items and acting on them all at once.
//!
//! Dragging a rectangle over the workspace's cards (starting from empty
//! space) selects them; while any are selected, the page is in selection
//! mode: the cards show selection circles and a click toggles one (see
//! `ItemGrid`), and the filter bar gives way to a `SelectionBar` of bulk
//! actions. Escape leaves selection mode.

use std::collections::HashMap;

use api::{ApiError, Collection, ListedItem, Tag};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::collections::COLLECTIONS_CSS;
use crate::filters::{FILTERS_CSS, TAG_LIST_LIMIT, TAG_SEARCH_DEBOUNCE};
use crate::i18n::api_error_message;
use crate::icons::{IconCheck, IconChevronDown, IconHeart, IconHeartFilled, IconTrash};
use crate::items::clean_tag_name;
use crate::viewer_nav::keep_step_keys;

const SELECTION_CSS: Asset = asset!("/assets/styling/selection.css");

/// Installs, once per page load, the document-level listeners behind the
/// marquee and Escape, and points them at this page's channel (so they
/// report to whichever page mounted last):
///
/// - A left-button drag that starts in the workspace (`.stash-main`) but
///   not on a card, a control or the filter bar draws a rectangle once it
///   has moved a few pixels, and reports the ids (`data-item-id`) of the
///   cards it intersects as they change. Shift, Ctrl or ⌘ held at the start
///   adds to the selection rather than replacing it. The click that ends
///   the drag is swallowed, so it can't also toggle a card.
/// - Escape, unless something else takes it: an open item, dialog or
///   popover.
///
/// Messages are `[kind, additive, ids]`.
const SELECTION_SCRIPT: &str = r#"
    window.__stashSelectionSend = (message) => dioxus.send(message);
    if (!window.__stashSelection) {
        window.__stashSelection = true;
        const send = (message) => {
            try {
                window.__stashSelectionSend(message);
            } catch (_) {}
        };
        const START_DISTANCE = 5;
        const NOT_EMPTY = "a, button, input, textarea, select, label, [role=button], [contenteditable], .item-card-shell, .stash-controls";
        let drag = null;

        const covered = (box) => {
            const ids = [];
            for (const card of document.querySelectorAll(".item-card-shell[data-item-id]")) {
                const r = card.getBoundingClientRect();
                if (r.right >= box.left && r.left <= box.right && r.bottom >= box.top && r.top <= box.bottom) {
                    ids.push(card.dataset.itemId);
                }
            }
            return ids;
        };
        const update = () => {
            // The start point moves with the page as it scrolls.
            const startX = drag.pageX - window.scrollX;
            const startY = drag.pageY - window.scrollY;
            if (!drag.rect) {
                if (Math.hypot(drag.x - startX, drag.y - startY) < START_DISTANCE) {
                    return;
                }
                drag.rect = document.createElement("div");
                drag.rect.className = "selection-marquee";
                document.body.appendChild(drag.rect);
                document.documentElement.classList.add("selection-dragging");
                window.getSelection()?.removeAllRanges();
                send(["start", drag.additive, []]);
            }
            const box = {
                left: Math.min(startX, drag.x),
                top: Math.min(startY, drag.y),
                right: Math.max(startX, drag.x),
                bottom: Math.max(startY, drag.y),
            };
            Object.assign(drag.rect.style, {
                left: `${box.left}px`,
                top: `${box.top}px`,
                width: `${box.right - box.left}px`,
                height: `${box.bottom - box.top}px`,
            });
            const ids = covered(box);
            const key = ids.join(" ");
            if (key !== drag.sent) {
                drag.sent = key;
                send(["covers", false, ids]);
            }
        };
        const end = () => {
            if (drag?.rect) {
                drag.rect.remove();
                document.documentElement.classList.remove("selection-dragging");
                const swallow = (e) => {
                    e.stopPropagation();
                    e.preventDefault();
                };
                window.addEventListener("click", swallow, { capture: true, once: true });
                setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
            }
            drag = null;
        };

        document.addEventListener("mousedown", (e) => {
            if (e.button !== 0 || !e.target.closest?.(".stash-main") || e.target.closest(NOT_EMPTY)) {
                return;
            }
            drag = {
                pageX: e.clientX + window.scrollX,
                pageY: e.clientY + window.scrollY,
                x: e.clientX,
                y: e.clientY,
                additive: e.shiftKey || e.ctrlKey || e.metaKey,
                rect: null,
                sent: null,
            };
        });
        document.addEventListener("mousemove", (e) => {
            if (!drag) {
                return;
            }
            // Released outside the window.
            if (!(e.buttons & 1)) {
                end();
                return;
            }
            drag.x = e.clientX;
            drag.y = e.clientY;
            update();
            if (drag.rect) {
                e.preventDefault();
            }
        });
        window.addEventListener("mouseup", end);
        window.addEventListener("scroll", () => {
            if (drag?.rect) {
                update();
            }
        }, { passive: true });
        document.addEventListener("keydown", (e) => {
            if (e.key !== "Escape") {
                return;
            }
            if (document.querySelector(".lightbox, .settings-overlay, .confirm-overlay")
                || e.target.closest?.(".filter-panel, .tag-picker, .collection-picker")) {
                return;
            }
            end();
            send(["escape", false, []]);
        });
    }
"#;

/// What the page's selection listeners report (see `SELECTION_SCRIPT`).
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum SelectionEvent {
    /// A rectangle started: it adds to the current selection, or replaces
    /// it.
    MarqueeStarted {
        additive: bool,
    },
    /// The ids of the cards the rectangle now covers.
    MarqueeCovers(Vec<String>),
    Escape,
}

/// Calls `on_event` with the page's `SelectionEvent`s for as long as the
/// calling component is mounted.
pub(crate) fn use_selection_events(on_event: impl FnMut(SelectionEvent) + 'static) {
    let on_event = use_callback(on_event);
    use_future(move || async move {
        let mut channel = document::eval(SELECTION_SCRIPT);
        while let Ok((kind, additive, ids)) = channel.recv::<(String, bool, Vec<String>)>().await {
            let event = match kind.as_str() {
                "start" => SelectionEvent::MarqueeStarted { additive },
                "covers" => SelectionEvent::MarqueeCovers(ids),
                "escape" => SelectionEvent::Escape,
                _ => continue,
            };
            on_event.call(event);
        }
    });
}

/// `base` plus the ids in `added` it doesn't have yet, in order.
pub(crate) fn merged(base: &[String], added: &[String]) -> Vec<String> {
    let mut ids = base.to_vec();
    for id in added {
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    ids
}

/// `ids` with `id` taken out, or added (last) if it wasn't there.
pub(crate) fn toggled_id(ids: &[String], id: &str) -> Vec<String> {
    if ids.iter().any(|existing| existing == id) {
        ids.iter()
            .filter(|existing| *existing != id)
            .cloned()
            .collect()
    } else {
        let mut ids = ids.to_vec();
        ids.push(id.to_string());
        ids
    }
}

/// `items` as last changed here: each replaced by its entry in `edits`, if
/// any (see `Home`'s local edits).
pub(crate) fn with_local_edits(
    items: &[ListedItem],
    edits: &HashMap<String, ListedItem>,
) -> Vec<ListedItem> {
    items
        .iter()
        .map(|item| edits.get(&item.id).unwrap_or(item).clone())
        .collect()
}

/// Whether every one of `items` is a favorite (none: no).
fn all_favorite(items: &[ListedItem]) -> bool {
    !items.is_empty() && items.iter().all(|item| item.is_favorite)
}

/// A tag's or collection's state across the selected items.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Check {
    /// Every selected item has it.
    Checked,
    /// Some do.
    Mixed,
    /// None does.
    Unchecked,
}

impl Check {
    fn of(having: usize, selected: usize) -> Self {
        match having {
            0 => Check::Unchecked,
            n if n >= selected => Check::Checked,
            _ => Check::Mixed,
        }
    }

    fn aria(self) -> &'static str {
        match self {
            Check::Checked => "true",
            Check::Mixed => "mixed",
            Check::Unchecked => "false",
        }
    }
}

/// Which of the item's labels a `BulkLabelMenu` edits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LabelKind {
    Tags,
    Collections,
}

impl LabelKind {
    fn title(self) -> String {
        match self {
            LabelKind::Tags => t!("tags-label"),
            LabelKind::Collections => t!("collections-label"),
        }
    }

    fn button_title(self) -> String {
        match self {
            LabelKind::Tags => t!("selection-tags-title"),
            LabelKind::Collections => t!("selection-collections-title"),
        }
    }

    fn search_placeholder(self) -> String {
        match self {
            LabelKind::Tags => t!("tags-search-placeholder"),
            LabelKind::Collections => t!("collections-search-placeholder"),
        }
    }

    fn none_yet(self) -> String {
        match self {
            LabelKind::Tags => t!("tags-none-yet"),
            LabelKind::Collections => t!("collections-none-yet"),
        }
    }

    fn no_matches(self) -> String {
        match self {
            LabelKind::Tags => t!("tags-no-matches"),
            LabelKind::Collections => t!("collections-no-matches"),
        }
    }

    fn load_failed(self, error: String) -> String {
        match self {
            LabelKind::Tags => t!("tags-load-failed", error: error),
            LabelKind::Collections => t!("collections-load-failed", error: error),
        }
    }

    fn create(self, name: &str) -> String {
        match self {
            LabelKind::Tags => t!("tags-create", name: name),
            LabelKind::Collections => t!("collections-create", name: name),
        }
    }

    /// The item's labels of this kind, as ids and names.
    fn of(self, item: &ListedItem) -> Vec<Tag> {
        match self {
            LabelKind::Tags => item.tags.clone(),
            LabelKind::Collections => item
                .collections
                .iter()
                .map(|collection| Tag {
                    id: collection.id.clone(),
                    name: collection.name.clone(),
                })
                .collect(),
        }
    }

    /// The user's labels of this kind containing `query`.
    async fn fetch(self, session: &AuthSession, query: String) -> Result<Vec<Tag>, ApiError> {
        match self {
            LabelKind::Tags => session.list_tags(query, TAG_LIST_LIMIT).await,
            LabelKind::Collections => Ok(session
                .list_collections(query, TAG_LIST_LIMIT)
                .await?
                .into_iter()
                .map(|collection| Tag {
                    id: collection.id,
                    name: collection.name,
                })
                .collect()),
        }
    }

    /// Checked: take it off every item that has it; otherwise put it on
    /// every item.
    fn change(self, check: Check, label: &Tag) -> BulkChange {
        match (self, check) {
            (LabelKind::Tags, Check::Checked) => BulkChange::RemoveTag(label.id.clone()),
            (LabelKind::Tags, _) => BulkChange::AddTag(label.name.clone()),
            (LabelKind::Collections, Check::Checked) => {
                BulkChange::RemoveFromCollection(label.id.clone())
            }
            (LabelKind::Collections, _) => BulkChange::AddToCollection(label.name.clone()),
        }
    }

    fn add_named(self, name: String) -> BulkChange {
        match self {
            LabelKind::Tags => BulkChange::AddTag(name),
            LabelKind::Collections => BulkChange::AddToCollection(name),
        }
    }
}

/// The labels of `kind` on any of `items`, with how many of them have each,
/// by name.
fn labels_on(kind: LabelKind, items: &[ListedItem]) -> Vec<(Tag, usize)> {
    let mut labels: Vec<(Tag, usize)> = Vec::new();
    for item in items {
        for label in kind.of(item) {
            match labels.iter_mut().find(|(known, _)| known.id == label.id) {
                Some((_, count)) => *count += 1,
                None => labels.push((label, 1)),
            }
        }
    }
    labels.sort_by_key(|(label, _)| label.name.to_lowercase());
    labels
}

/// One change applied to every selected item.
#[derive(Clone, Debug, PartialEq)]
enum BulkChange {
    /// By name: the server finds the tag, ignoring case, or creates it.
    AddTag(String),
    RemoveTag(String),
    /// By name, like a tag.
    AddToCollection(String),
    RemoveFromCollection(String),
    Favorite(bool),
}

impl BulkChange {
    fn is_favorite(&self) -> bool {
        matches!(self, BulkChange::Favorite(_))
    }

    /// Whether `item` already is as this change would leave it: it's then
    /// skipped.
    fn already_applied(&self, item: &ListedItem) -> bool {
        let named = |name: &str, other: &str| name.to_lowercase() == other.to_lowercase();
        match self {
            BulkChange::AddTag(name) => item.tags.iter().any(|tag| named(&tag.name, name)),
            BulkChange::RemoveTag(id) => !item.tags.iter().any(|tag| tag.id == *id),
            BulkChange::AddToCollection(name) => item
                .collections
                .iter()
                .any(|collection| named(&collection.name, name)),
            BulkChange::RemoveFromCollection(id) => !item
                .collections
                .iter()
                .any(|collection| collection.id == *id),
            BulkChange::Favorite(favorite) => item.is_favorite == *favorite,
        }
    }

    /// Saves the change to `item`, and returns the item as it now is.
    async fn apply(
        &self,
        session: &AuthSession,
        item: &ListedItem,
    ) -> Result<ListedItem, ApiError> {
        let mut item = item.clone();
        match self {
            BulkChange::AddTag(name) => {
                let tag = session.assign_tag(item.id.clone(), name.clone()).await?;
                item.tags.push(tag);
                // As the server lists them.
                item.tags.sort_by_key(|tag| tag.name.to_lowercase());
            }
            BulkChange::RemoveTag(id) => {
                session.remove_tag(item.id.clone(), id.clone()).await?;
                item.tags.retain(|tag| tag.id != *id);
            }
            BulkChange::AddToCollection(name) => {
                let collection: Collection = session
                    .add_to_collection(item.id.clone(), name.clone())
                    .await?;
                item.collections.push(collection);
                item.collections.sort_by(|a, b| a.name.cmp(&b.name));
            }
            BulkChange::RemoveFromCollection(id) => {
                session
                    .remove_from_collection(item.id.clone(), id.clone())
                    .await?;
                item.collections.retain(|collection| collection.id != *id);
            }
            BulkChange::Favorite(favorite) => {
                session.set_favorite(item.id.clone(), *favorite).await?;
                item.is_favorite = *favorite;
            }
        }
        Ok(item)
    }
}

/// The filter bar's stand-in in selection mode: `[ N selected  Cancel ]` on
/// the left, `[ Tags ▾ ] [ Collections ▾ ] [ ♥ Favorites ] [ Delete ]` on
/// the right, acting on `items` (the selected ones, as shown).
///
/// Tags and Collections open a list of the user's tags or collections, each
/// checked if every selected item has it, mixed if some do: picking a
/// checked one takes it off them all, any other puts it on them all. A
/// typed name that doesn't exist yet can be created, on them all.
/// Favorites removes them all from favorites if they all are, and adds them
/// all otherwise. A change is saved item by item, skipping the items it
/// wouldn't change; `on_labels_changed` or `on_favorites_changed` then gets
/// the items changed, as they now are (also after a failure part way, which
/// is shown here).
///
/// Cancel calls `on_cancel`, and Delete `on_delete` (which asks first).
#[component]
pub(crate) fn SelectionBar(
    items: Vec<ListedItem>,
    on_cancel: EventHandler<()>,
    on_labels_changed: EventHandler<Vec<ListedItem>>,
    on_favorites_changed: EventHandler<Vec<ListedItem>>,
    on_delete: EventHandler<()>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut busy = use_signal(|| false);
    let mut error = use_signal(|| None::<String>);
    let mut open = use_signal(|| None::<LabelKind>);

    let apply = use_callback({
        let items = items.clone();
        move |change: BulkChange| {
            if busy() {
                return;
            }
            let session = session.clone();
            let targets: Vec<ListedItem> = items
                .iter()
                .filter(|item| !change.already_applied(item))
                .cloned()
                .collect();
            if targets.is_empty() {
                return;
            }
            spawn(async move {
                busy.set(true);
                error.set(None);
                let mut changed = Vec::new();
                let mut failure = None;
                for item in &targets {
                    match change.apply(&session, item).await {
                        Ok(updated) => changed.push(updated),
                        Err(err) => failure = Some(err),
                    }
                }
                if let Some(err) = failure {
                    error.set(Some(t!(
                        "selection-failed",
                        error: api_error_message(&err)
                    )));
                }
                if change.is_favorite() {
                    on_favorites_changed.call(changed);
                } else {
                    on_labels_changed.call(changed);
                }
                busy.set(false);
            });
        }
    });

    let count = items.len();
    let favorites = all_favorite(&items);
    let favorite_title = if favorites {
        t!("selection-favorite-remove")
    } else {
        t!("selection-favorite-add")
    };

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }
        document::Link { rel: "stylesheet", href: SELECTION_CSS }

        div { class: "selection-summary",
            span { class: "selection-count", {t!("selection-count", count: count)} }
            button {
                class: "selection-cancel",
                r#type: "button",
                title: t!("selection-cancel-title"),
                onclick: move |_| on_cancel.call(()),
                {t!("common-cancel")}
            }
            if let Some(message) = error() {
                span { class: "selection-error", "{message}" }
            }
        }
        div { class: "selection-actions",
            for kind in [LabelKind::Tags, LabelKind::Collections] {
                BulkLabelMenu {
                    key: "{kind:?}",
                    kind,
                    items: items.clone(),
                    open: open() == Some(kind),
                    busy: busy(),
                    on_open: move |opened: bool| open.set(opened.then_some(kind)),
                    on_change: apply,
                }
            }
            button {
                class: if favorites { "filter-button selection-button filter-button-active selection-favorites-active" } else { "filter-button selection-button" },
                r#type: "button",
                title: favorite_title.clone(),
                aria_label: favorite_title,
                aria_pressed: if favorites { "true" } else { "false" },
                disabled: busy(),
                onclick: move |_| apply.call(BulkChange::Favorite(!favorites)),
                if favorites {
                    IconHeartFilled {}
                } else {
                    IconHeart {}
                }
                span { {t!("nav-favorites")} }
            }
            button {
                class: "filter-button selection-button selection-delete",
                r#type: "button",
                title: t!("selection-delete-title"),
                disabled: busy(),
                onclick: move |_| {
                    open.set(None);
                    on_delete.call(());
                },
                IconTrash {}
                span { {t!("item-delete")} }
            }
        }
    }
}

/// `[ Tags ▾ ]` or `[ Collections ▾ ]` in the `SelectionBar`, and while
/// `open`, its list (see `BulkLabelOptions`). `on_open` asks to open or
/// close it.
#[component]
fn BulkLabelMenu(
    kind: LabelKind,
    items: Vec<ListedItem>,
    open: bool,
    busy: bool,
    on_open: EventHandler<bool>,
    on_change: EventHandler<BulkChange>,
) -> Element {
    rsx! {
        div { class: "filter-control",
            button {
                class: if open { "filter-button selection-button filter-button-active" } else { "filter-button selection-button" },
                r#type: "button",
                title: kind.button_title(),
                aria_haspopup: "dialog",
                aria_expanded: if open { "true" } else { "false" },
                onclick: move |_| on_open.call(!open),
                span { "{kind.title()}" }
                span { class: "filter-chevron", IconChevronDown {} }
            }
            if open {
                div { class: "filter-backdrop", onclick: move |_| on_open.call(false) }
                div {
                    class: "filter-panel selection-panel",
                    role: "dialog",
                    aria_label: kind.title(),
                    onkeydown: move |evt| {
                        if evt.key() == Key::Escape {
                            on_open.call(false);
                        }
                    },
                    BulkLabelOptions { kind, items, busy, on_change }
                }
            }
        }
    }
}

/// The open list of a `BulkLabelMenu`: a search box, then the matching tags
/// or collections, each a three-state checkbox for the selected `items`
/// (see `SelectionBar`). With nothing typed, the ones on any selected item
/// when it opened come first (so they stay put as they're changed), then
/// the rest of the user's. "Create …" (or Enter) puts a typed name no
/// existing one has on every selected item. Mounted only while open, so it
/// fetches the current list each time.
#[component]
fn BulkLabelOptions(
    kind: LabelKind,
    items: Vec<ListedItem>,
    busy: bool,
    on_change: EventHandler<BulkChange>,
) -> Element {
    let session = use_context::<AuthSession>();
    let mut query = use_signal(String::new);
    let current = labels_on(kind, &items);
    let pinned: Vec<Tag> = use_hook({
        let current = current.clone();
        move || current.into_iter().map(|(label, _)| label).collect()
    });

    // Re-runs as the query changes; the delay debounces typing.
    let matches = use_resource(move || {
        let session = session.clone();
        let query = clean_tag_name(&query());
        async move {
            if !query.is_empty() {
                Delay::new(TAG_SEARCH_DEBOUNCE).await;
            }
            kind.fetch(&session, query).await
        }
    });

    let typed = clean_tag_name(&query());
    let check = |label: &Tag| {
        let having = current
            .iter()
            .find(|(on_items, _)| on_items.id == label.id)
            .map_or(0, |(_, count)| *count);
        Check::of(having, items.len())
    };
    let found: Option<Vec<Tag>> = match &*matches.read() {
        Some(Ok(found)) => Some(found.clone()),
        _ => None,
    };
    let options: Vec<Tag> = match &found {
        Some(found) if !typed.is_empty() => found.clone(),
        found => pinned
            .iter()
            .cloned()
            .chain(
                found
                    .iter()
                    .flatten()
                    .filter(|label| !pinned.iter().any(|p| p.id == label.id))
                    .cloned(),
            )
            .collect(),
    };
    let exact = options
        .iter()
        .find(|label| label.name.to_lowercase() == typed.to_lowercase())
        .cloned();
    let can_create = !typed.is_empty() && found.is_some() && exact.is_none();
    // Enter: the exact match, as if clicked, or a new one.
    let enter_change = match &exact {
        Some(label) => Some(kind.change(check(label), label)),
        None if can_create => Some(kind.add_named(typed.clone())),
        None => None,
    };
    let rows: Vec<(Tag, Check)> = options
        .into_iter()
        .map(|label| {
            let state = check(&label);
            (label, state)
        })
        .collect();

    rsx! {
        document::Link { rel: "stylesheet", href: COLLECTIONS_CSS }

        input {
            class: "filters-search",
            r#type: "search",
            placeholder: kind.search_placeholder(),
            maxlength: "50",
            value: "{query}",
            oninput: move |evt| query.set(evt.value()),
            onmounted: move |evt| async move {
                let _ = evt.set_focus(true).await;
            },
            onkeydown: move |evt: KeyboardEvent| {
                keep_step_keys(&evt);
                if evt.key() == Key::Enter {
                    evt.prevent_default();
                    if busy {
                        return;
                    }
                    if let Some(change) = enter_change.clone() {
                        on_change.call(change);
                        query.set(String::new());
                    }
                }
            },
        }
        div { class: "filters-options",
            for (label , state) in rows.iter().cloned() {
                button {
                    class: "filter-option selection-option",
                    key: "{label.id}",
                    r#type: "button",
                    role: "checkbox",
                    aria_checked: state.aria(),
                    disabled: busy,
                    onclick: move |_| on_change.call(kind.change(state, &label)),
                    span {
                        class: match state {
                            Check::Checked => "selection-check selection-check-on",
                            Check::Mixed => "selection-check selection-check-mixed",
                            Check::Unchecked => "selection-check",
                        },
                        match state {
                            Check::Checked => rsx! { IconCheck {} },
                            Check::Mixed => rsx! { span { class: "selection-check-dash" } },
                            Check::Unchecked => rsx! {},
                        }
                    }
                    span { class: "selection-option-name", "{label.name}" }
                }
            }
            if can_create {
                button {
                    class: "collection-picker-create",
                    r#type: "button",
                    disabled: busy,
                    onclick: {
                        let typed = typed.clone();
                        move |_| {
                            on_change.call(kind.add_named(typed.clone()));
                            query.set(String::new());
                        }
                    },
                    "{kind.create(&typed)}"
                }
            }
            match &*matches.read() {
                Some(Err(err)) => rsx! {
                    p { class: "filter-empty", "{kind.load_failed(api_error_message(err))}" }
                },
                None if rows.is_empty() => rsx! {
                    p { class: "filter-empty", {t!("common-loading")} }
                },
                Some(Ok(_)) if rows.is_empty() && !can_create => rsx! {
                    p { class: "filter-empty",
                        if typed.is_empty() { "{kind.none_yet()}" } else { "{kind.no_matches()}" }
                    }
                },
                _ => rsx! {},
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(list: &[&str]) -> Vec<String> {
        list.iter().map(|id| id.to_string()).collect()
    }

    fn tag(id: &str, name: &str) -> Tag {
        Tag {
            id: id.to_string(),
            name: name.to_string(),
        }
    }

    fn item(id: &str, tags: &[Tag], is_favorite: bool) -> ListedItem {
        ListedItem {
            id: id.to_string(),
            r#type: "text".to_string(),
            status: "ready".to_string(),
            created_at: "2026-09-24T12:21:14Z".to_string(),
            text: Some(id.to_string()),
            download_url: None,
            thumbnail_url: None,
            file: None,
            tags: tags.to_vec(),
            collections: Vec::new(),
            is_favorite,
        }
    }

    #[test]
    fn a_marquee_adds_what_it_covers_after_the_base_once() {
        assert_eq!(
            merged(&ids(&["a", "b"]), &ids(&["b", "c"])),
            ids(&["a", "b", "c"])
        );
        assert_eq!(merged(&[], &ids(&["c", "a"])), ids(&["c", "a"]));
    }

    #[test]
    fn toggling_an_id_adds_or_removes_it() {
        assert_eq!(toggled_id(&ids(&["a"]), "b"), ids(&["a", "b"]));
        assert_eq!(toggled_id(&ids(&["a", "b"]), "a"), ids(&["b"]));
    }

    #[test]
    fn a_label_is_checked_when_all_have_it_and_mixed_when_some_do() {
        assert_eq!(Check::of(3, 3), Check::Checked);
        assert_eq!(Check::of(1, 3), Check::Mixed);
        assert_eq!(Check::of(0, 3), Check::Unchecked);
    }

    #[test]
    fn labels_on_the_selection_are_counted_and_sorted_by_name() {
        let trips = tag("t1", "trips");
        let books = tag("t2", "Books");
        let items = [
            item("a", &[trips.clone(), books.clone()], false),
            item("b", &[trips.clone()], false),
        ];
        assert_eq!(
            labels_on(LabelKind::Tags, &items),
            vec![(books, 1), (trips, 2)]
        );
    }

    #[test]
    fn checked_labels_are_removed_and_the_rest_added() {
        let trips = tag("t1", "Trips");
        assert_eq!(
            LabelKind::Tags.change(Check::Checked, &trips),
            BulkChange::RemoveTag("t1".to_string())
        );
        for state in [Check::Mixed, Check::Unchecked] {
            assert_eq!(
                LabelKind::Tags.change(state, &trips),
                BulkChange::AddTag("Trips".to_string())
            );
            assert_eq!(
                LabelKind::Collections.change(state, &trips),
                BulkChange::AddToCollection("Trips".to_string())
            );
        }
        assert_eq!(
            LabelKind::Collections.change(Check::Checked, &trips),
            BulkChange::RemoveFromCollection("t1".to_string())
        );
    }

    #[test]
    fn items_a_change_would_not_change_are_skipped() {
        let trips = tag("t1", "Trips");
        let tagged = item("a", &[trips.clone()], true);
        let untagged = item("b", &[], false);

        let add = BulkChange::AddTag("trips".to_string());
        assert!(add.already_applied(&tagged));
        assert!(!add.already_applied(&untagged));

        let remove = BulkChange::RemoveTag("t1".to_string());
        assert!(!remove.already_applied(&tagged));
        assert!(remove.already_applied(&untagged));

        assert!(BulkChange::Favorite(true).already_applied(&tagged));
        assert!(!BulkChange::Favorite(true).already_applied(&untagged));
    }

    #[test]
    fn favorites_are_removed_only_when_every_selected_item_is_one() {
        assert!(all_favorite(&[item("a", &[], true), item("b", &[], true)]));
        assert!(!all_favorite(&[
            item("a", &[], true),
            item("b", &[], false)
        ]));
        assert!(!all_favorite(&[]));
    }

    #[test]
    fn local_edits_replace_the_fetched_items() {
        let fetched = [item("a", &[], false), item("b", &[], false)];
        let edits = HashMap::from([("b".to_string(), item("b", &[], true))]);
        let shown = with_local_edits(&fetched, &edits);
        assert!(!shown[0].is_favorite);
        assert!(shown[1].is_favorite);
    }
}
