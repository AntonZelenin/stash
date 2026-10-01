//! Product analytics for the Stash clients, sent straight to PostHog's
//! capture API (never through the Stash API). See "Product analytics" in
//! docs/architecture.md for what's tracked and why.
//!
//! - `events`: the events and their properties. Typed, so nothing but the
//!   allowed properties, with closed sets of values, can be sent: no
//!   content, filenames, URLs, queries, tag names or ids.
//! - `session`: when a session starts (opening the app, or coming back
//!   after 30 minutes without any activity).
//! - `tracker`: identity, a bounded queue and PostHog's batch payload.
//! - `transport`: `Sender`, one batch request at a time, abandoned after a
//!   timeout.
//!
//! Nothing here captures anything by itself: there's no autocapture,
//! pageviews, session replay or exception capture, only the events the app
//! records explicitly.

mod config;
mod events;
mod session;
mod tracker;
mod transport;

pub use config::{AnalyticsConfig, ConfigError};
pub use events::{DisplayMode, Event, FilterCategories, ItemType, Platform, Sort};
pub use session::{SESSION_IDLE_TIMEOUT, SessionClock};
pub use tracker::{MAX_BATCH_EVENTS, MAX_QUEUED_EVENTS, Tracker};
pub use transport::{SEND_TIMEOUT, Sender};
