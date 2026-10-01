//! Product analytics in the app: the `analytics` crate's tracker, provided
//! to the component tree by `use_init_analytics` and used through
//! `use_analytics`.
//!
//! - Sessions: opening the app starts one; after that only the user's own
//!   activity (`ActivityTracker`: pointer, key and wheel input anywhere,
//!   plus every tracked action) moves the session clock, so rerenders,
//!   background refreshes and a tab left open never extend or start one.
//!   The first activity after 30 idle minutes starts the next.
//! - Identity: events are for the signed-in account's `analytics_id`
//!   (`identify`, once `GET /users/me` has answered), the same id the API
//!   uses, on every device. Events from before that wait for it; signing
//!   out forgets the identity and drops anything still waiting.
//! - Sending: straight to PostHog, never through the API, one batch at a
//!   time from a bounded queue, each request abandoned after a timeout.
//!   Failures only lose events; nothing about analytics can block or
//!   break what the user is doing.
//!
//! Off (every call does nothing) unless the platform crate was built with
//! analytics configured; see `AnalyticsConfig`.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

pub use ::analytics::{AnalyticsConfig, ConfigError, Platform};
use ::analytics::{
    DisplayMode as AnalyticsMode, Event, FilterCategories, ItemType, SessionClock, Sort, Tracker,
};
use api::ItemSort;
use chrono::Utc;
use dioxus::prelude::*;

use crate::AuthSession;
use crate::preferences::{DisplayMode, Preferences};

struct State {
    platform: Platform,
    sender: ::analytics::Sender,
    tracker: Tracker,
    session: SessionClock,
    /// For each event's `mode`.
    preferences: Option<Preferences>,
    /// A send loop is running: it takes whatever is queued, so captures
    /// meanwhile don't start another.
    sending: bool,
}

/// The app's analytics; cheap to clone. Does nothing when analytics is off.
#[derive(Clone, Default)]
pub struct Analytics {
    state: Option<Rc<RefCell<State>>>,
}

impl PartialEq for Analytics {
    fn eq(&self, other: &Self) -> bool {
        match (&self.state, &other.state) {
            (Some(a), Some(b)) => Rc::ptr_eq(a, b),
            (None, None) => true,
            _ => false,
        }
    }
}

impl Analytics {
    fn new(
        config: Option<AnalyticsConfig>,
        platform: Platform,
        preferences: Option<Preferences>,
    ) -> Self {
        Self {
            state: config.map(|config| {
                Rc::new(RefCell::new(State {
                    platform,
                    sender: ::analytics::Sender::new(config),
                    tracker: Tracker::default(),
                    session: SessionClock::default(),
                    preferences,
                    sending: false,
                }))
            }),
        }
    }

    /// Whether this build sends analytics at all.
    pub fn is_enabled(&self) -> bool {
        self.state.is_some()
    }

    /// The user did something: starts a session if none is running (see
    /// the module docs). Called for all input by `ActivityTracker`, and by
    /// every tracked action.
    pub fn activity(&self) {
        let Some(state) = &self.state else {
            return;
        };
        {
            let mut state = state.borrow_mut();
            let now = Utc::now();
            if state.session.activity(now.timestamp_millis()) {
                let event = Event::SessionStarted {
                    platform: state.platform,
                    mode: state.mode(),
                };
                state.tracker.capture(event, now);
            }
        }
        self.flush();
    }

    /// The account events are for, from now on (its `analytics_id`).
    /// Another account than before (switched without the app noticing a
    /// sign-out) starts a new session for it.
    pub fn identify(&self, analytics_id: &str) {
        let Some(state) = &self.state else {
            return;
        };
        let switched = {
            let mut state = state.borrow_mut();
            let switched = state
                .tracker
                .identity()
                .is_some_and(|current| current != analytics_id && !analytics_id.is_empty());
            if switched {
                state.tracker.reset();
                state.session.reset();
            }
            state.tracker.identify(analytics_id);
            switched
        };
        if switched {
            // The new account's own session.
            self.activity();
        }
        self.flush();
    }

    /// Signed out: no one is identified any more, events waiting for an
    /// identity are dropped, and the next activity starts a new session.
    pub fn reset(&self) {
        let Some(state) = &self.state else {
            return;
        };
        let mut state = state.borrow_mut();
        state.tracker.reset();
        state.session.reset();
    }

    fn track(&self, event: Event) {
        let Some(state) = &self.state else {
            return;
        };
        // An action is activity too (and may start the session it's in).
        self.activity();
        state.borrow_mut().tracker.capture(event, Utc::now());
        self.flush();
    }

    /// The Normal/Blind mode was switched to `mode`.
    pub fn mode_changed(&self, mode: DisplayMode) {
        self.track(Event::ModeChanged {
            mode: analytics_mode(mode),
        });
    }

    /// An item of `item_type` (the API's name for it) was opened: in the
    /// viewer, or a file or link opened from its card. `from_search`: from
    /// search results rather than the list.
    pub fn item_opened(&self, item_type: &str, from_search: bool) {
        let Some(item_type) = ItemType::from_api(item_type) else {
            return;
        };
        let Some(state) = &self.state else {
            return;
        };
        let mode = state.borrow().mode();
        self.track(Event::ItemOpened {
            item_type,
            from_search,
            mode,
        });
    }

    /// The listing order was changed to `sort`.
    pub fn sort_changed(&self, sort: ItemSort) {
        let sort = match sort {
            ItemSort::Newest => Sort::NewestFirst,
            ItemSort::Oldest => Sort::OldestFirst,
            ItemSort::Random => Sort::Random,
        };
        self.track(Event::SortChanged { sort });
    }

    /// The type, tag or favourites filters changed; these are the ones now
    /// active.
    pub fn filters_changed(&self, filters: FilterCategories) {
        self.track(Event::FiltersChanged { filters });
    }

    pub fn account_settings_opened(&self) {
        self.track(Event::AccountSettingsOpened);
    }

    pub fn privacy_opened(&self) {
        self.track(Event::PrivacyOpened);
    }

    /// Starts sending what's ready, unless a send loop is already running
    /// or no one is identified yet. Runs detached from any component, so
    /// closing whatever triggered it doesn't cancel a request in flight.
    fn flush(&self) {
        let Some(state) = self.state.clone() else {
            return;
        };
        {
            let mut current = state.borrow_mut();
            if current.sending || !current.tracker.has_ready() {
                return;
            }
            current.sending = true;
        }
        dioxus::core::spawn_forever(async move {
            loop {
                // Never borrowed across the await.
                let next = {
                    let mut current = state.borrow_mut();
                    let api_key = current.sender.api_key().to_string();
                    current
                        .tracker
                        .take_batch(&api_key)
                        .map(|body| (current.sender.clone(), body))
                };
                let Some((sender, body)) = next else {
                    break;
                };
                // Lost on failure: analytics is best effort.
                sender.send(body).await;
            }
            state.borrow_mut().sending = false;
        });
    }
}

impl State {
    fn mode(&self) -> AnalyticsMode {
        analytics_mode(
            self.preferences
                .as_ref()
                .map(Preferences::current_display_mode)
                .unwrap_or_default(),
        )
    }
}

fn analytics_mode(mode: DisplayMode) -> AnalyticsMode {
    match mode {
        DisplayMode::Normal => AnalyticsMode::Normal,
        DisplayMode::Blind => AnalyticsMode::Blind,
    }
}

/// Sets up analytics for the component tree below the caller, and starts
/// the first session (opening the app). Call once, in the app root, after
/// `use_init_preferences` and providing the `AuthSession` (if the app has
/// sign-in). `config`: None turns it off.
pub fn use_init_analytics(config: Option<AnalyticsConfig>, platform: Platform) -> Analytics {
    let analytics = use_context_provider(|| {
        Analytics::new(config, platform, try_consume_context::<Preferences>())
    });
    use_hook({
        let analytics = analytics.clone();
        move || analytics.activity()
    });

    // Signing out (from any signed-in state) forgets the user. Not on
    // opening the app signed out: the session that started then waits for
    // whoever signs in.
    let session = use_hook(try_consume_context::<AuthSession>);
    let was_signed_in = use_hook(|| Rc::new(Cell::new(false)));
    {
        let analytics = analytics.clone();
        use_effect(move || {
            let Some(session) = &session else {
                return;
            };
            let signed_in = session.is_authenticated();
            if was_signed_in.replace(signed_in) && !signed_in {
                analytics.reset();
            }
        });
    }
    analytics
}

/// The app's analytics (does nothing if there's none, e.g. in tests).
pub(crate) fn use_analytics() -> Analytics {
    use_hook(|| try_consume_context::<Analytics>().unwrap_or_default())
}

/// Reports the user's input anywhere inside it as activity (see the module
/// docs). Lays nothing out itself (`display: contents`). Wrap the app's
/// router in it.
#[component]
pub fn ActivityTracker(children: Element) -> Element {
    let analytics = use_analytics();
    let on_pointer = analytics.clone();
    let on_key = analytics.clone();
    let on_wheel = analytics;
    rsx! {
        div {
            style: "display: contents",
            onpointerdown: move |_| on_pointer.activity(),
            onkeydown: move |_| on_key.activity(),
            onwheel: move |_| on_wheel.activity(),
            {children}
        }
    }
}
