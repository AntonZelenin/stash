use std::time::Duration;

/// How long without any activity ends a session: the next activity starts
/// a new one.
pub const SESSION_IDLE_TIMEOUT: Duration = Duration::from_secs(30 * 60);

/// Decides when a session starts. It only learns about activity (the user
/// interacting with the app, told by `activity`): rerenders, timers,
/// background refreshes and a hidden tab never reach it, so time spent
/// idle or in the background never extends a session.
#[derive(Clone, Debug, Default)]
pub struct SessionClock {
    /// When the last activity was, in milliseconds since the Unix epoch;
    /// None before any (the app just opened, or the clock was reset).
    last_activity_ms: Option<i64>,
}

impl SessionClock {
    /// Records activity at `now_ms`; true if it starts a session: the
    /// first activity since the clock started (opening the app counts), or
    /// the first after `SESSION_IDLE_TIMEOUT` without any. A clock that
    /// went backwards counts as continuing.
    pub fn activity(&mut self, now_ms: i64) -> bool {
        let starts = match self.last_activity_ms {
            None => true,
            Some(last) => now_ms.saturating_sub(last) >= SESSION_IDLE_TIMEOUT.as_millis() as i64,
        };
        self.last_activity_ms = Some(match self.last_activity_ms {
            Some(last) if now_ms < last => last,
            _ => now_ms,
        });
        starts
    }

    /// Forgets the session (e.g. on sign-out): the next activity starts one.
    pub fn reset(&mut self) {
        self.last_activity_ms = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINUTE: i64 = 60_000;

    #[test]
    fn opening_the_app_starts_a_session() {
        assert!(SessionClock::default().activity(1_000));
    }

    #[test]
    fn activity_within_the_timeout_continues_it() {
        let mut clock = SessionClock::default();
        clock.activity(0);
        // Every 29 minutes for hours: one session.
        for step in 1..10 {
            assert!(!clock.activity(step * 29 * MINUTE));
        }
    }

    #[test]
    fn coming_back_after_30_idle_minutes_starts_another() {
        let mut clock = SessionClock::default();
        clock.activity(0);
        assert!(!clock.activity(29 * MINUTE + 59_999));
        assert!(clock.activity(29 * MINUTE + 59_999 + 30 * MINUTE));
    }

    #[test]
    fn checking_again_is_not_activity() {
        // Only `activity` moves the clock: an hour in the background, then
        // activity, is a new session however often the app rerendered.
        let mut clock = SessionClock::default();
        clock.activity(0);
        assert!(clock.activity(60 * MINUTE));
    }

    #[test]
    fn a_clock_going_backwards_continues_the_session() {
        let mut clock = SessionClock::default();
        clock.activity(10 * MINUTE);
        assert!(!clock.activity(5 * MINUTE));
        assert!(!clock.activity(20 * MINUTE));
    }

    #[test]
    fn reset_starts_over() {
        let mut clock = SessionClock::default();
        clock.activity(0);
        clock.reset();
        assert!(clock.activity(MINUTE));
    }
}
