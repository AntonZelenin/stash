use std::collections::VecDeque;

use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};

use crate::events::Event;

/// Most events kept waiting at once, identified and not together: past
/// it, the oldest are dropped. Analytics never grows without bound, e.g.
/// while PostHog can't be reached.
pub const MAX_QUEUED_EVENTS: usize = 100;
/// Most events sent in one request.
pub const MAX_BATCH_EVENTS: usize = 50;

/// Who events are for, and the events waiting to be sent.
///
/// Events are only ever sent for an identified user: the account's
/// `analytics_id` (from `GET /users/me`), the same id the API sends its
/// events with, on every device. Events captured before that's known
/// (opening the app, before the account loads) wait for it, and are
/// dropped if the user signs out first (`reset`). Nothing identifies the
/// user beyond that id: no email, no account id.
#[derive(Debug, Default)]
pub struct Tracker {
    identity: Option<String>,
    unidentified: VecDeque<(Event, DateTime<Utc>)>,
    /// Ready to send: PostHog batch entries, already for their user.
    ready: VecDeque<Value>,
}

impl Tracker {
    pub fn identity(&self) -> Option<&str> {
        self.identity.as_deref()
    }

    /// Records `event`, which happened at `at`.
    pub fn capture(&mut self, event: Event, at: DateTime<Utc>) {
        match self.identity.clone() {
            Some(id) => self.push_ready(entry(&event, &id, at)),
            None => {
                self.unidentified.push_back((event, at));
                self.trim();
            }
        }
    }

    /// From now on, events are for `analytics_id`, and so are those
    /// waiting for an identity. Another id than the current one is a
    /// different account: the waiting events (if any) go to the new one,
    /// those already queued stay with the one they were captured for.
    /// Returns whether the identity changed. An empty id (an API that
    /// doesn't send one) identifies no one.
    pub fn identify(&mut self, analytics_id: &str) -> bool {
        if analytics_id.is_empty() || self.identity.as_deref() == Some(analytics_id) {
            return false;
        }
        self.identity = Some(analytics_id.to_string());
        while let Some((event, at)) = self.unidentified.pop_front() {
            self.push_ready(entry(&event, analytics_id, at));
        }
        true
    }

    /// Signed out: no one is identified, and events still waiting for an
    /// identity are dropped (they'd otherwise go to whoever signs in
    /// next). Events already captured for the previous user are still
    /// sent, as theirs.
    pub fn reset(&mut self) {
        self.identity = None;
        self.unidentified.clear();
    }

    pub fn has_ready(&self) -> bool {
        !self.ready.is_empty()
    }

    /// The next request body for PostHog's batch endpoint (at most
    /// `MAX_BATCH_EVENTS` events), taken off the queue; None if nothing's
    /// ready. Sent at most once: a batch that fails isn't retried.
    pub fn take_batch(&mut self, api_key: &str) -> Option<String> {
        if self.ready.is_empty() {
            return None;
        }
        let count = self.ready.len().min(MAX_BATCH_EVENTS);
        let batch: Vec<Value> = self.ready.drain(..count).collect();
        Some(json!({ "api_key": api_key, "batch": batch }).to_string())
    }

    fn push_ready(&mut self, entry: Value) {
        self.ready.push_back(entry);
        self.trim();
    }

    fn trim(&mut self) {
        while self.ready.len() + self.unidentified.len() > MAX_QUEUED_EVENTS {
            if self.unidentified.pop_front().is_none() {
                self.ready.pop_front();
            }
        }
    }
}

/// One event in PostHog's batch format, for `analytics_id`. Besides the
/// event's own properties, only `$geoip_disable` (no location looked up
/// from the IP) and `$lib`: PostHog adds nothing else for this API (no
/// URL, referrer, screen or browser properties).
fn entry(event: &Event, analytics_id: &str, at: DateTime<Utc>) -> Value {
    let mut properties = event.properties();
    properties.insert("distinct_id".into(), analytics_id.into());
    properties.insert("$geoip_disable".into(), true.into());
    properties.insert("$lib".into(), "stash-client".into());
    json!({
        "event": event.name(),
        "timestamp": at.to_rfc3339_opts(SecondsFormat::Millis, true),
        "properties": properties,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::tests::{allowed, every_event};
    use crate::events::{DisplayMode, Platform};

    fn at() -> DateTime<Utc> {
        DateTime::from_timestamp(1_800_000_000, 0).unwrap()
    }

    fn started() -> Event {
        Event::SessionStarted {
            platform: Platform::Web,
            mode: DisplayMode::Normal,
        }
    }

    fn sent(tracker: &mut Tracker) -> Vec<Value> {
        let mut events = Vec::new();
        while let Some(body) = tracker.take_batch("phc_key") {
            let body: Value = serde_json::from_str(&body).unwrap();
            assert_eq!(body["api_key"], "phc_key");
            events.extend(body["batch"].as_array().unwrap().iter().cloned());
        }
        events
    }

    #[test]
    fn nothing_is_sent_before_the_user_is_identified() {
        let mut tracker = Tracker::default();
        tracker.capture(started(), at());

        assert!(tracker.take_batch("phc_key").is_none());

        assert!(tracker.identify("a1"));
        let events = sent(&mut tracker);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["event"], "session_started");
        assert_eq!(events[0]["properties"]["distinct_id"], "a1");
        assert_eq!(events[0]["timestamp"], "2027-01-15T08:00:00.000Z");
    }

    #[test]
    fn sign_out_drops_what_no_one_was_identified_for() {
        let mut tracker = Tracker::default();
        tracker.identify("alice");
        tracker.capture(Event::AccountSettingsOpened, at());
        tracker.reset();
        // Before the next account has loaded.
        tracker.capture(started(), at());
        tracker.reset();
        tracker.capture(Event::PrivacyOpened, at());
        tracker.identify("bob");

        let events = sent(&mut tracker);
        let sent: Vec<(&str, &str)> = events
            .iter()
            .map(|e| {
                (
                    e["event"].as_str().unwrap(),
                    e["properties"]["distinct_id"].as_str().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            sent,
            [
                ("account_settings_opened", "alice"),
                ("privacy_opened", "bob")
            ]
        );
    }

    #[test]
    fn identifying_again_changes_nothing() {
        let mut tracker = Tracker::default();
        assert!(tracker.identify("a1"));
        assert!(!tracker.identify("a1"));
        assert!(!tracker.identify(""));
        assert_eq!(tracker.identity(), Some("a1"));
    }

    #[test]
    fn the_queue_is_bounded() {
        let mut tracker = Tracker::default();
        for _ in 0..MAX_QUEUED_EVENTS + 25 {
            tracker.capture(Event::PrivacyOpened, at());
        }
        tracker.identify("a1");
        for _ in 0..10 {
            tracker.capture(Event::AccountSettingsOpened, at());
        }

        let events = sent(&mut tracker);
        assert_eq!(events.len(), MAX_QUEUED_EVENTS);
        // The newest are kept.
        assert_eq!(events.last().unwrap()["event"], "account_settings_opened");
    }

    #[test]
    fn batches_are_bounded() {
        let mut tracker = Tracker::default();
        tracker.identify("a1");
        for _ in 0..MAX_BATCH_EVENTS + 1 {
            tracker.capture(Event::PrivacyOpened, at());
        }
        let first: Value = serde_json::from_str(&tracker.take_batch("k").unwrap()).unwrap();
        assert_eq!(first["batch"].as_array().unwrap().len(), MAX_BATCH_EVENTS);
        assert!(tracker.has_ready());
    }

    #[test]
    fn sends_only_allowed_properties() {
        let mut tracker = Tracker::default();
        tracker.identify("a1");
        for event in every_event() {
            tracker.capture(event, at());
        }
        for entry in sent(&mut tracker) {
            let name = entry["event"].as_str().unwrap();
            let allowed = allowed(name);
            for (property, _) in entry["properties"].as_object().unwrap() {
                assert!(
                    allowed.contains(&property.as_str())
                        || ["distinct_id", "$geoip_disable", "$lib"].contains(&property.as_str()),
                    "{name}: {property}"
                );
            }
            // Nothing that would identify the user or what they have.
            for forbidden in [
                "$ip",
                "$current_url",
                "email",
                "$set",
                "$pathname",
                "$referrer",
            ] {
                assert!(
                    entry["properties"].get(forbidden).is_none(),
                    "{name}: {forbidden}"
                );
            }
        }
    }
}
