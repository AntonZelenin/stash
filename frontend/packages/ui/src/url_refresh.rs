//! Fresh storage URLs for an item whose listed ones have expired, and how
//! its media shows while they can't be loaded.
//!
//! A listing's pre-signed `download_url` and `thumbnail_url` stop working
//! after their TTL (or earlier, when the credentials that signed them
//! expire), while a tab can stay open for days. So an item's card owns a
//! `UrlRefresh`: its media reports loading and failing, and a failure
//! fetches the item again (`GET /items/{id}`) for a new URL, which then
//! replaces the failed one wherever the item is shown. Nothing refreshes
//! ahead of time: only a failure to load starts a refresh.
//!
//! A failure starts a cycle (`RefreshState`): a refresh right away and, if
//! that request fails with a network or server error, up to
//! `RETRY_DELAYS.len()` retries, each that long after the last request
//! failed; then it stops, with the failure shown and a retry button. A 404
//! (the item is gone) stops refreshing it. What keeps a URL that's broken
//! for good (the object is gone, the format can't be shown) from looping:
//! - A URL that failed never starts a cycle again, and isn't loaded again:
//!   its media shows the failure instead.
//! - A URL a refresh fetched that fails within `FRESH_URL_GRACE` can't
//!   have expired, so it doesn't start a cycle. A listed URL, or a fetched
//!   one failing later (it expired), does, however often that happens.
//! - One cycle at a time per item.
//! - At most `MAX_CONCURRENT_REFRESHES` automatic requests are in flight
//!   at once, across all items; the rest queue.
//!
//! The retry button (`UrlRefresh::retry`) always fetches right away,
//! ignoring all of that. Opening a file fetches its URL fresh every time,
//! apart from all of the above.
//!
//! Video and audio players fetch their own URLs; see `media.rs`.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::pin::Pin;
use std::task::{Context, Poll, Waker};
use std::time::Duration;

use api::{ApiError, ListedItem};
use dioxus::core::{Runtime, ScopeId, current_scope_id};
use dioxus::prelude::*;
use dioxus_i18n::t;
use futures_timer::Delay;

use crate::AuthSession;
use crate::toast::show_toast;

/// A cycle's retries after a request failed with a network or server
/// error: each this long after the failed request finished. Then it stops.
const RETRY_DELAYS: [Duration; 2] = [Duration::from_secs(5), Duration::from_secs(20)];

/// A URL a refresh fetched that fails within this long hasn't expired
/// (they last an hour): it's broken, so it doesn't start another cycle.
const FRESH_URL_GRACE: Duration = Duration::from_secs(5 * 60);

/// Automatic refreshes in flight at once, across the whole UI.
const MAX_CONCURRENT_REFRESHES: usize = 4;

/// Milliseconds since the Unix epoch.
type Millis = i64;

fn now() -> Millis {
    chrono::Utc::now().timestamp_millis()
}

fn millis(duration: Duration) -> Millis {
    duration.as_millis() as Millis
}

/// An item's pre-signed URLs.
#[derive(Clone, Debug, Default, PartialEq)]
struct ItemUrls {
    download_url: Option<String>,
    thumbnail_url: Option<String>,
}

impl ItemUrls {
    fn of(item: &ListedItem) -> Self {
        Self {
            download_url: item.download_url.clone(),
            thumbnail_url: item.thumbnail_url.clone(),
        }
    }

    fn get(&self, kind: UrlKind) -> Option<&str> {
        match kind {
            UrlKind::Download => self.download_url.as_deref(),
            UrlKind::Thumbnail => self.thumbnail_url.as_deref(),
        }
    }

    fn slot(&mut self, kind: UrlKind) -> &mut Option<String> {
        match kind {
            UrlKind::Download => &mut self.download_url,
            UrlKind::Thumbnail => &mut self.thumbnail_url,
        }
    }

    fn kind_of(&self, url: &str) -> Option<UrlKind> {
        UrlKind::ALL
            .into_iter()
            .find(|&kind| self.get(kind) == Some(url))
    }
}

/// Which of an item's URLs.
#[derive(Clone, Copy, Debug, PartialEq)]
enum UrlKind {
    Download,
    Thumbnail,
}

impl UrlKind {
    const ALL: [UrlKind; 2] = [UrlKind::Download, UrlKind::Thumbnail];
}

/// How a refresh went.
#[derive(Clone, Debug, PartialEq)]
enum Outcome {
    Fetched(ItemUrls),
    /// 404: the item no longer exists.
    Gone,
    /// Network or server trouble (or 429): worth trying again shortly.
    Retryable,
    /// Anything else (e.g. signed out): not worth trying again by itself.
    Failed,
}

impl Outcome {
    fn of(result: &Result<Option<ListedItem>, ApiError>) -> Self {
        match result {
            Ok(Some(item)) => Outcome::Fetched(ItemUrls::of(item)),
            Ok(None) => Outcome::Gone,
            Err(ApiError::Network | ApiError::Server | ApiError::RateLimited) => Outcome::Retryable,
            Err(_) => Outcome::Failed,
        }
    }
}

/// How an item's media shows from a URL.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum MediaStatus {
    /// Load it (it hasn't failed).
    Load,
    /// It failed, and new URLs are on their way.
    Refreshing,
    /// It failed (or there's none), and no request is under way: show the
    /// failure, with a retry button.
    Failed,
}

/// An automatic refresh cycle under way.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Cycle {
    /// Requests made so far (the first one included).
    attempts: u32,
    /// One is queued or in flight (otherwise it's waiting to retry).
    in_flight: bool,
}

/// What's known about an item's URLs since it was listed with `listed`.
/// Times are passed in, so the rules can be tested without waiting.
#[derive(Clone, Debug, Default, PartialEq)]
struct RefreshState {
    listed: ItemUrls,
    /// The URLs to use: the listed ones, as replaced by refreshes.
    current: ItemUrls,
    /// When a refresh fetched the current download and thumbnail URLs;
    /// None for listed ones.
    download_fetched_at: Option<Millis>,
    thumbnail_fetched_at: Option<Millis>,
    /// Current URLs that failed to load.
    broken: Vec<String>,
    automatic: Option<Cycle>,
    /// A retry the user asked for is in flight.
    manual: bool,
    /// The item no longer exists.
    gone: bool,
}

impl RefreshState {
    fn new(listed: ItemUrls) -> Self {
        Self {
            current: listed.clone(),
            listed,
            ..Self::default()
        }
    }

    fn fetched_at(&mut self, kind: UrlKind) -> &mut Option<Millis> {
        match kind {
            UrlKind::Download => &mut self.download_fetched_at,
            UrlKind::Thumbnail => &mut self.thumbnail_fetched_at,
        }
    }

    fn is_broken(&self, url: &str) -> bool {
        self.broken.iter().any(|broken| broken == url)
    }

    fn requesting(&self) -> bool {
        self.manual || self.automatic.is_some_and(|cycle| cycle.in_flight)
    }

    fn status(&self, url: Option<&str>) -> MediaStatus {
        match url {
            Some(url) if !self.is_broken(url) => MediaStatus::Load,
            _ if self.requesting() => MediaStatus::Refreshing,
            _ => MediaStatus::Failed,
        }
    }

    /// `url` loaded.
    fn loaded(&mut self, url: &str) {
        self.broken.retain(|broken| broken != url);
    }

    /// `url` failed to load. True if that starts a cycle (its first request
    /// is then to be made).
    fn failed(&mut self, url: &str, now: Millis) -> bool {
        // One no longer in use (already replaced), or already known.
        let Some(kind) = self.current.kind_of(url) else {
            return false;
        };
        if self.is_broken(url) {
            return false;
        }
        self.broken.push(url.to_string());
        let just_fetched = self
            .fetched_at(kind)
            .is_some_and(|at| now - at < millis(FRESH_URL_GRACE));
        // A cycle under way replaces every broken URL when it succeeds.
        if self.gone || self.manual || self.automatic.is_some() || just_fetched {
            return false;
        }
        self.automatic = Some(Cycle {
            attempts: 1,
            in_flight: true,
        });
        true
    }

    /// The cycle's request finished at `now`. Returns when to retry, if it
    /// failed in a way worth retrying and retries are left; otherwise the
    /// cycle is over.
    fn automatic_done(&mut self, outcome: Outcome, now: Millis) -> Option<Millis> {
        let cycle = self.automatic.take()?;
        match outcome {
            Outcome::Fetched(fresh) => self.replace_broken(fresh, now),
            Outcome::Gone => self.gone = true,
            Outcome::Retryable => {
                let delay = RETRY_DELAYS.get(cycle.attempts as usize - 1)?;
                self.automatic = Some(Cycle {
                    in_flight: false,
                    ..cycle
                });
                return Some(now + millis(*delay));
            }
            Outcome::Failed => {}
        }
        None
    }

    /// Time for the cycle's retry. True if it's still wanted (nothing ended
    /// the cycle meanwhile): its request is then to be made.
    fn retry_due(&mut self) -> bool {
        match self.automatic {
            Some(cycle) if !cycle.in_flight && !self.manual => {
                self.automatic = Some(Cycle {
                    attempts: cycle.attempts + 1,
                    in_flight: true,
                });
                true
            }
            // The user's retry under way takes over.
            Some(cycle) if !cycle.in_flight => {
                self.automatic = None;
                false
            }
            _ => false,
        }
    }

    /// Replaces the broken URLs (and any missing ones, e.g. a thumbnail
    /// made since) with `fresh`'s, fetched at `now`, keeping the ones that
    /// work: those don't have to load again.
    fn replace_broken(&mut self, fresh: ItemUrls, now: Millis) {
        for kind in UrlKind::ALL {
            let broken = self.current.get(kind).is_none_or(|url| self.is_broken(url));
            if broken && let Some(url) = fresh.get(kind) {
                *self.current.slot(kind) = Some(url.to_string());
                *self.fetched_at(kind) = Some(now);
            }
        }
        let current = self.current.clone();
        self.broken.retain(|url| current.kind_of(url).is_some());
    }

    fn start_manual(&mut self) {
        self.manual = true;
    }

    /// The user's retry finished at `now`: success replaces all the URLs
    /// and ends any cycle waiting to retry.
    fn manual_done(&mut self, outcome: Outcome, now: Millis) {
        self.manual = false;
        match outcome {
            Outcome::Fetched(fresh) => {
                self.current = fresh;
                self.download_fetched_at = Some(now);
                self.thumbnail_fetched_at = Some(now);
                self.broken.clear();
                if self.automatic.is_some_and(|cycle| !cycle.in_flight) {
                    self.automatic = None;
                }
                self.gone = false;
            }
            Outcome::Gone => self.gone = true,
            Outcome::Retryable | Outcome::Failed => {}
        }
    }
}

/// An item's URLs as currently known, and the refreshing of them (see the
/// module docs). Made by `use_url_refresh` in the component that owns the
/// item (its card), whose scope its requests run in, so they finish even if
/// what reported the failure (a closed viewer) is gone by then.
#[derive(Clone, PartialEq)]
pub(crate) struct UrlRefresh {
    item_id: String,
    listed: ItemUrls,
    state: Signal<RefreshState>,
    /// The listed URLs and those in use instead, if any differ: what the
    /// owner renders, so bookkeeping (e.g. an image loading) doesn't
    /// re-render it.
    replaced: Memo<Option<(ItemUrls, ItemUrls)>>,
    owner: ScopeId,
}

/// A `UrlRefresh` for `item`, as listed. A new listing (other URLs) starts
/// over, its URLs replacing any refreshed ones.
pub(crate) fn use_url_refresh(item: &ListedItem) -> UrlRefresh {
    let state = use_signal(RefreshState::default);
    let replaced = use_memo(move || {
        let state = state.read();
        (state.current != state.listed).then(|| (state.listed.clone(), state.current.clone()))
    });
    let owner = use_hook(current_scope_id);
    UrlRefresh {
        item_id: item.id.clone(),
        listed: ItemUrls::of(item),
        state,
        replaced,
        owner,
    }
}

impl UrlRefresh {
    /// `item` with the URLs to use now: the listed ones, or those that
    /// replaced them. Re-renders the caller when they're replaced.
    pub(crate) fn apply(&self, item: ListedItem) -> ListedItem {
        let urls = match (self.replaced)() {
            Some((listed, current)) if listed == self.listed => current,
            _ => self.listed.clone(),
        };
        ListedItem {
            download_url: urls.download_url,
            thumbnail_url: urls.thumbnail_url,
            ..item
        }
    }

    /// How media from `url` (None: the item has none) shows. Re-renders the
    /// caller when that changes.
    pub(crate) fn status(&self, url: Option<&str>) -> MediaStatus {
        let state = self.state.read();
        if state.listed == self.listed {
            state.status(url)
        } else {
            RefreshState::new(self.listed.clone()).status(url)
        }
    }

    /// Media loaded from `url`.
    pub(crate) fn loaded(&self, url: &str) {
        self.update(|state| state.loaded(url));
    }

    /// Media failed to load from `url`: refreshes it if that's allowed now.
    pub(crate) fn failed(&self, url: &str) {
        if self.update(|state| state.failed(url, now())) {
            let this = self.clone();
            let session = consume_context::<AuthSession>();
            self.spawn(async move { this.run_automatic(session).await });
        }
    }

    /// The user's retry: fetches new URLs right away, whatever the
    /// automatic refreshes are waiting for.
    pub(crate) fn retry(&self) {
        self.update(RefreshState::start_manual);
        let this = self.clone();
        let session = consume_context::<AuthSession>();
        self.spawn(async move {
            let outcome = Outcome::of(&session.get_item(this.item_id.clone()).await);
            this.update(|state| state.manual_done(outcome, now()));
        });
    }

    /// A cycle's requests, each once there's room for it, and its retries
    /// after network or server errors (while the owner is shown).
    async fn run_automatic(&self, session: AuthSession) {
        loop {
            let result = {
                let _slot = acquire_refresh_slot().await;
                session.get_item(self.item_id.clone()).await
            };
            let Some(at) = self.update(|state| state.automatic_done(Outcome::of(&result), now()))
            else {
                return;
            };
            Delay::new(Duration::from_millis((at - now()).max(0) as u64)).await;
            if !self.update(RefreshState::retry_due) {
                return;
            }
        }
    }

    /// Opens the file in a new tab (where the browser shows it or downloads
    /// it) from a URL fetched just now, rather than the listed one, which
    /// may have expired. Shows a toast if it can't. Independent of the
    /// refreshing above.
    ///
    /// Call it from the click itself: on the web, the tab has to be opened
    /// before waiting for the URL, or the browser blocks it as a pop-up.
    pub(crate) fn open_download(&self) {
        let tab = NewTab::open();
        let item_id = self.item_id.clone();
        let session = consume_context::<AuthSession>();
        self.spawn(async move {
            let url = match session.get_item(item_id).await {
                Ok(Some(item)) => item.download_url,
                Ok(None) | Err(_) => None,
            };
            if !tab.show(url).await {
                show_toast(t!("file-open-failed"));
            }
        });
    }

    /// Applies `change` to the state for the current listing, starting it
    /// over first if the listing changed.
    fn update<T>(&self, change: impl FnOnce(&mut RefreshState) -> T) -> T {
        let mut signal = self.state;
        let mut state = signal.write();
        if state.listed != self.listed {
            *state = RefreshState::new(self.listed.clone());
        }
        change(&mut state)
    }

    fn spawn(&self, task: impl Future<Output = ()> + 'static) {
        Runtime::current().spawn(self.owner, task);
    }
}

/// An item's image from `url` (None: it has none), with `class`; `lazy`
/// loads it only once it's near the screen. Reports loading to `urls`, and
/// while it can't be loaded shows a box with `status_class` instead: empty
/// while new URLs are on their way, then a failure message and a retry
/// button (`retry_class`).
#[component]
pub(crate) fn RefreshingImage(
    url: Option<String>,
    urls: UrlRefresh,
    class: &'static str,
    alt: String,
    #[props(default)] lazy: bool,
    status_class: &'static str,
    retry_class: &'static str,
) -> Element {
    match (urls.status(url.as_deref()), url) {
        (MediaStatus::Load, Some(url)) => rsx! {
            img {
                class,
                src: "{url}",
                alt,
                loading: if lazy { "lazy" } else { "eager" },
                onload: {
                    let (urls, url) = (urls.clone(), url.clone());
                    move |_| urls.loaded(&url)
                },
                onerror: move |_| urls.failed(&url),
            }
        },
        (MediaStatus::Load | MediaStatus::Refreshing, _) => rsx! {
            div { class: status_class, aria_busy: "true" }
        },
        (MediaStatus::Failed, _) => rsx! {
            MediaFailed { urls, status_class, retry_class }
        },
    }
}

/// Media that can't be loaded: a message and a retry button, in a box with
/// `status_class`.
#[component]
pub(crate) fn MediaFailed(
    urls: UrlRefresh,
    status_class: &'static str,
    retry_class: &'static str,
) -> Element {
    rsx! {
        div { class: status_class, role: "alert",
            p { {t!("media-load-failed")} }
            button {
                class: retry_class,
                r#type: "button",
                // Not also opening what the media sits in (a card's viewer),
                // by click or by Enter/Space (see `OpenButton`).
                onclick: move |evt| {
                    evt.stop_propagation();
                    urls.retry();
                },
                onkeydown: move |evt| evt.stop_propagation(),
                {t!("item-media-retry")}
            }
        }
    }
}

/// A tab to show a URL in once it's known.
///
/// On the web, a blank tab opened right away (still within the click, so
/// it's not blocked as a pop-up), then pointed at the URL. Elsewhere the
/// app's webview opens external URLs in the system browser by itself, so
/// nothing has to happen up front.
struct NewTab(document::Eval);

impl NewTab {
    #[cfg(target_arch = "wasm32")]
    fn open() -> Self {
        Self(document::eval(
            r#"
            const tab = window.open("", "_blank");
            if (tab) tab.opener = null;
            const url = await dioxus.recv();
            if (!url) {
                if (tab) tab.close();
                return false;
            }
            if (tab) {
                tab.location.href = url;
                return true;
            }
            const late = window.open(url, "_blank");
            if (late) late.opener = null;
            return late !== null;
            "#,
        ))
    }

    #[cfg(not(target_arch = "wasm32"))]
    fn open() -> Self {
        Self(document::eval(
            r#"
            const url = await dioxus.recv();
            if (!url) return false;
            window.location.href = url;
            return true;
            "#,
        ))
    }

    /// Shows `url` in it, or closes it if None. False if nothing opened.
    async fn show(self, url: Option<String>) -> bool {
        let has_url = url.is_some();
        if self.0.send(url).is_err() {
            return false;
        }
        // Can't tell: assume it opened, rather than wrongly report failure.
        has_url && self.0.join::<bool>().await.unwrap_or(true)
    }
}

/// Admits at most `max` holders at a time; the rest wait, first come
/// first served. Tickets say who is who.
#[derive(Debug)]
struct Limiter {
    max: usize,
    active: usize,
    next_ticket: u64,
    waiting: VecDeque<(u64, Option<Waker>)>,
    /// Admitted, but not yet told.
    admitted: Vec<u64>,
}

impl Limiter {
    fn new(max: usize) -> Self {
        Self {
            max,
            active: 0,
            next_ticket: 0,
            waiting: VecDeque::new(),
            admitted: Vec::new(),
        }
    }

    /// A ticket, and whether it's admitted right away.
    fn enter(&mut self) -> (u64, bool) {
        let ticket = self.next_ticket;
        self.next_ticket += 1;
        if self.active < self.max && self.waiting.is_empty() {
            self.active += 1;
            (ticket, true)
        } else {
            self.waiting.push_back((ticket, None));
            (ticket, false)
        }
    }

    /// Whether `ticket` has been admitted since; if not, `waker` is woken
    /// when it is.
    fn check(&mut self, ticket: u64, waker: Option<&Waker>) -> bool {
        if let Some(index) = self
            .admitted
            .iter()
            .position(|&admitted| admitted == ticket)
        {
            self.admitted.swap_remove(index);
            return true;
        }
        if let Some((_, slot)) = self
            .waiting
            .iter_mut()
            .find(|(waiting, _)| *waiting == ticket)
        {
            *slot = waker.cloned();
        }
        false
    }

    /// A holder is done: admits the next one waiting.
    fn leave(&mut self) {
        self.active -= 1;
        while self.active < self.max
            && let Some((ticket, waker)) = self.waiting.pop_front()
        {
            self.active += 1;
            self.admitted.push(ticket);
            if let Some(waker) = waker {
                waker.wake();
            }
        }
    }

    /// `ticket` gave up (its task was dropped) before being told.
    fn cancel(&mut self, ticket: u64) {
        if let Some(index) = self
            .waiting
            .iter()
            .position(|(waiting, _)| *waiting == ticket)
        {
            self.waiting.remove(index);
        } else if let Some(index) = self
            .admitted
            .iter()
            .position(|&admitted| admitted == ticket)
        {
            self.admitted.swap_remove(index);
            self.leave();
        }
    }
}

thread_local! {
    /// Automatic refreshes across the whole UI (it runs on one thread).
    static REFRESH_LIMITER: RefCell<Limiter> = RefCell::new(Limiter::new(MAX_CONCURRENT_REFRESHES));
}

/// Held while an automatic refresh is in flight.
struct RefreshSlot;

impl Drop for RefreshSlot {
    fn drop(&mut self) {
        REFRESH_LIMITER.with(|limiter| limiter.borrow_mut().leave());
    }
}

/// Waits for room for an automatic refresh.
fn acquire_refresh_slot() -> AcquireSlot {
    let (ticket, admitted) = REFRESH_LIMITER.with(|limiter| limiter.borrow_mut().enter());
    AcquireSlot { ticket, admitted }
}

struct AcquireSlot {
    ticket: u64,
    /// Admitted and told, so a drop has nothing to undo.
    admitted: bool,
}

impl Future for AcquireSlot {
    type Output = RefreshSlot;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<RefreshSlot> {
        let ticket = self.ticket;
        if self.admitted
            || REFRESH_LIMITER.with(|limiter| limiter.borrow_mut().check(ticket, Some(cx.waker())))
        {
            // Handed to the `RefreshSlot`, which leaves on drop.
            self.admitted = false;
            self.ticket = u64::MAX;
            return Poll::Ready(RefreshSlot);
        }
        Poll::Pending
    }
}

impl Drop for AcquireSlot {
    fn drop(&mut self) {
        if self.ticket == u64::MAX {
            return;
        }
        let ticket = self.ticket;
        let admitted = self.admitted;
        REFRESH_LIMITER.with(|limiter| {
            let mut limiter = limiter.borrow_mut();
            if admitted {
                limiter.leave();
            } else {
                limiter.cancel(ticket);
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECOND: Millis = 1000;
    const MINUTE: Millis = 60 * SECOND;
    const HOUR: Millis = 60 * MINUTE;

    fn urls(generation: u32) -> ItemUrls {
        ItemUrls {
            download_url: Some(format!("https://storage/original?sig={generation}")),
            thumbnail_url: Some(format!("https://storage/thumb?sig={generation}")),
        }
    }

    fn original(state: &RefreshState) -> String {
        state.current.download_url.clone().unwrap()
    }

    fn thumbnail(state: &RefreshState) -> String {
        state.current.thumbnail_url.clone().unwrap()
    }

    /// `state`'s cycle getting URLs of `generation` at `now`.
    fn refreshed(state: &mut RefreshState, generation: u32, now: Millis) {
        assert_eq!(
            state.automatic_done(Outcome::Fetched(urls(generation)), now),
            None
        );
        assert_eq!(state.automatic, None);
    }

    #[test]
    fn an_expired_url_is_refreshed_right_away() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&thumbnail(&state), 0));
        assert_eq!(
            state.status(Some(&thumbnail(&state))),
            MediaStatus::Refreshing
        );
        refreshed(&mut state, 1, SECOND);
        assert_eq!(thumbnail(&state), urls(1).thumbnail_url.unwrap());
        assert_eq!(state.status(Some(&thumbnail(&state))), MediaStatus::Load);
    }

    #[test]
    fn the_full_size_image_opens_long_after_a_thumbnail_refresh() {
        let mut state = RefreshState::new(urls(0));
        // The thumbnail expired and was refreshed; the original, never
        // shown, stays as listed.
        assert!(state.failed(&thumbnail(&state), 0));
        refreshed(&mut state, 1, SECOND);
        state.loaded(&thumbnail(&state));
        assert_eq!(original(&state), urls(0).download_url.unwrap());

        // Days later, the viewer opens: the original has long expired.
        let later = 3 * 24 * HOUR;
        assert!(state.failed(&original(&state), later));
        refreshed(&mut state, 2, later + SECOND);
        assert_eq!(original(&state), urls(2).download_url.unwrap());
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Load);
        // The thumbnail kept working, so it's kept.
        assert_eq!(thumbnail(&state), urls(1).thumbnail_url.unwrap());
    }

    #[test]
    fn a_refreshed_url_that_never_loaded_is_refreshed_again_once_expired() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        refreshed(&mut state, 1, SECOND);
        // The viewer closed before the new original loaded; an hour on
        // it's opened again.
        assert!(state.failed(&original(&state), HOUR));
    }

    #[test]
    fn expiry_is_refreshed_every_hour_for_days() {
        let mut state = RefreshState::new(urls(0));
        for hour in 1..=24 * 7 {
            let now = hour * HOUR;
            let url = if hour % 2 == 0 {
                original(&state)
            } else {
                thumbnail(&state)
            };
            assert!(state.failed(&url, now), "hour {hour}");
            refreshed(&mut state, hour as u32, now + SECOND);
            let fresh = if hour % 2 == 0 {
                original(&state)
            } else {
                thumbnail(&state)
            };
            assert_ne!(fresh, url);
            state.loaded(&fresh);
        }
    }

    #[test]
    fn a_network_error_is_retried_after_5_then_20_seconds_then_stops() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        // Each retry counts from when the failed request finished.
        assert_eq!(
            state.automatic_done(Outcome::Retryable, 30 * SECOND),
            Some(35 * SECOND)
        );
        // Shown as failed (retryable by hand) while waiting.
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Failed);

        assert!(state.retry_due());
        assert_eq!(
            state.status(Some(&original(&state))),
            MediaStatus::Refreshing
        );
        assert_eq!(
            state.automatic_done(Outcome::Retryable, 36 * SECOND),
            Some(56 * SECOND)
        );

        assert!(state.retry_due());
        assert_eq!(state.automatic_done(Outcome::Retryable, 57 * SECOND), None);
        assert_eq!(state.automatic, None);
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Failed);
        // No more by itself: that URL is done.
        assert!(!state.retry_due());
        assert!(!state.failed(&original(&state), HOUR));
    }

    #[test]
    fn a_network_error_then_recovery() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        assert_eq!(
            state.automatic_done(Outcome::Retryable, SECOND),
            Some(6 * SECOND)
        );
        assert!(state.retry_due());
        refreshed(&mut state, 1, 7 * SECOND);
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Load);
    }

    #[test]
    fn a_later_failure_starts_a_fresh_cycle_after_one_gave_up() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        state.automatic_done(Outcome::Retryable, SECOND);
        assert!(state.retry_due());
        state.automatic_done(Outcome::Retryable, 7 * SECOND);
        assert!(state.retry_due());
        assert_eq!(state.automatic_done(Outcome::Retryable, 28 * SECOND), None);

        // The thumbnail, listed, fails later on: a new cycle, which fixes
        // both.
        assert!(state.failed(&thumbnail(&state), HOUR));
        refreshed(&mut state, 1, HOUR + SECOND);
        assert_eq!(state.current, urls(1));
    }

    #[test]
    fn permanently_broken_media_costs_one_request_then_fails() {
        let mut state = RefreshState::new(urls(0));
        let mut requests = 0;
        // The original is gone for good: every URL for it fails right away.
        // Its viewer is opened once a minute for a day.
        for minute in 0..24 * 60 {
            let now = minute * MINUTE;
            if state.failed(&original(&state), now) {
                requests += 1;
                refreshed(&mut state, minute as u32 + 1, now + SECOND);
                // The fresh URL fails as soon as it's tried.
                assert!(!state.failed(&original(&state), now + 2 * SECOND));
            }
        }
        assert_eq!(requests, 1);
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Failed);
    }

    #[test]
    fn a_fetched_url_failing_within_the_grace_doesnt_start_a_cycle() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        refreshed(&mut state, 1, 0);
        let grace = millis(FRESH_URL_GRACE);
        assert!(!state.failed(&original(&state), grace - 1));

        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        refreshed(&mut state, 1, 0);
        assert!(state.failed(&original(&state), grace));
    }

    #[test]
    fn a_working_thumbnail_doesnt_let_a_broken_original_loop() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        refreshed(&mut state, 1, SECOND);
        state.loaded(&thumbnail(&state));
        assert!(!state.failed(&original(&state), 2 * SECOND));
    }

    #[test]
    fn the_same_failed_url_never_refreshes_twice() {
        let mut state = RefreshState::new(urls(0));
        let url = original(&state);
        assert!(state.failed(&url, 0));
        state.automatic_done(Outcome::Failed, SECOND);
        assert!(!state.failed(&url, HOUR));
    }

    #[test]
    fn only_one_cycle_at_a_time() {
        // While its request is in flight...
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&thumbnail(&state), 0));
        assert!(!state.failed(&original(&state), 0));
        // ...which replaces both.
        refreshed(&mut state, 1, SECOND);
        assert_eq!(state.current, urls(1));

        // And while it waits to retry.
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&thumbnail(&state), 0));
        state.automatic_done(Outcome::Retryable, SECOND);
        assert!(!state.failed(&original(&state), 2 * SECOND));
        assert!(state.retry_due());
        refreshed(&mut state, 1, 7 * SECOND);
        assert_eq!(state.current, urls(1));
    }

    #[test]
    fn a_stale_url_failing_is_ignored() {
        let mut state = RefreshState::new(urls(0));
        let old = thumbnail(&state);
        assert!(state.failed(&old, 0));
        refreshed(&mut state, 1, SECOND);
        assert!(!state.failed(&old, HOUR));
    }

    #[test]
    fn a_deleted_item_stops_refreshing() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&thumbnail(&state), 0));
        assert_eq!(state.automatic_done(Outcome::Gone, SECOND), None);
        assert!(!state.failed(&original(&state), HOUR));
        assert_eq!(state.status(Some(&thumbnail(&state))), MediaStatus::Failed);
    }

    #[test]
    fn a_manual_retry_goes_ahead_while_waiting_to_retry() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        assert!(state.automatic_done(Outcome::Retryable, SECOND).is_some());

        state.start_manual();
        assert_eq!(
            state.status(Some(&original(&state))),
            MediaStatus::Refreshing
        );
        state.manual_done(Outcome::Fetched(urls(1)), 2 * SECOND);
        assert_eq!(state.current, urls(1));
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Load);
        // Its success ended the cycle: the pending retry does nothing.
        assert!(!state.retry_due());
        assert_eq!(state.automatic, None);
    }

    #[test]
    fn a_retry_due_during_a_manual_retry_leaves_it_to_that() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        state.automatic_done(Outcome::Retryable, SECOND);
        state.start_manual();
        assert!(!state.retry_due());
        state.manual_done(Outcome::Retryable, 7 * SECOND);
        assert_eq!(state.automatic, None);
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Failed);
    }

    #[test]
    fn a_successful_manual_retry_resets_the_failure_state() {
        // Both fail; the refresh replaces both, and both new ones fail
        // straight away, so both show as failed.
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        assert!(!state.failed(&thumbnail(&state), 0));
        refreshed(&mut state, 1, SECOND);
        assert!(!state.failed(&original(&state), 2 * SECOND));
        assert!(!state.failed(&thumbnail(&state), 2 * SECOND));
        assert_eq!(state.status(Some(&thumbnail(&state))), MediaStatus::Failed);

        state.start_manual();
        state.manual_done(Outcome::Fetched(urls(2)), 3 * SECOND);
        assert_eq!(state.current, urls(2));
        assert!(state.broken.is_empty());
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Load);
        // Once they've had time to expire, a failure starts a cycle again.
        assert!(state.failed(&original(&state), HOUR));
    }

    #[test]
    fn a_failed_manual_retry_leaves_the_failure_shown() {
        let mut state = RefreshState::new(urls(0));
        assert!(state.failed(&original(&state), 0));
        state.automatic_done(Outcome::Failed, SECOND);
        state.start_manual();
        state.manual_done(Outcome::Retryable, 2 * SECOND);
        assert_eq!(state.status(Some(&original(&state))), MediaStatus::Failed);
    }

    #[test]
    fn an_item_without_a_url_shows_as_failed() {
        let state = RefreshState::new(ItemUrls::default());
        assert_eq!(state.status(None), MediaStatus::Failed);
    }

    #[test]
    fn the_limiter_admits_four_and_queues_the_rest_in_order() {
        let mut limiter = Limiter::new(MAX_CONCURRENT_REFRESHES);
        let tickets: Vec<(u64, bool)> = (0..50).map(|_| limiter.enter()).collect();
        assert_eq!(tickets.iter().filter(|(_, admitted)| *admitted).count(), 4);
        assert!(tickets[..4].iter().all(|(_, admitted)| *admitted));
        assert!(!limiter.check(tickets[4].0, None));

        limiter.leave();
        assert!(limiter.check(tickets[4].0, None));
        assert!(!limiter.check(tickets[5].0, None));
        assert_eq!(limiter.active, 4);
    }

    #[test]
    fn a_dropped_waiter_gives_up_its_place() {
        let mut limiter = Limiter::new(1);
        let (_first, _) = limiter.enter();
        let (second, _) = limiter.enter();
        let (third, _) = limiter.enter();
        limiter.cancel(second);
        limiter.leave();
        assert!(limiter.check(third, None));
        assert_eq!(limiter.active, 1);
    }

    #[test]
    fn an_admitted_waiter_dropped_before_running_frees_its_slot() {
        let mut limiter = Limiter::new(1);
        let (_first, _) = limiter.enter();
        let (second, _) = limiter.enter();
        let (third, _) = limiter.enter();
        limiter.leave(); // admits `second`, which is dropped unaware
        limiter.cancel(second);
        assert!(limiter.check(third, None));
        assert_eq!(limiter.active, 1);
    }

    #[test]
    fn many_stale_items_never_have_more_than_four_refreshes_in_flight() {
        let waker = Waker::noop();
        let mut cx = Context::from_waker(waker);
        let mut pending: Vec<Pin<Box<AcquireSlot>>> =
            (0..40).map(|_| Box::pin(acquire_refresh_slot())).collect();
        let mut running = Vec::new();
        let mut max_in_flight = 0;
        while !pending.is_empty() || !running.is_empty() {
            pending.retain_mut(|acquire| match acquire.as_mut().poll(&mut cx) {
                Poll::Ready(slot) => {
                    running.push(slot);
                    false
                }
                Poll::Pending => true,
            });
            max_in_flight = max_in_flight.max(running.len());
            // One request finishes.
            running.pop();
        }
        assert_eq!(max_in_flight, MAX_CONCURRENT_REFRESHES);
        REFRESH_LIMITER.with(|limiter| assert_eq!(limiter.borrow().active, 0));
    }
}
