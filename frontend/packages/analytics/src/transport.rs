use std::time::Duration;

use futures_timer::Delay;
use futures_util::future::{Either, select};

use crate::config::AnalyticsConfig;

/// How long one batch request may take before it's abandoned (and its
/// events dropped).
pub const SEND_TIMEOUT: Duration = Duration::from_secs(5);

/// Sends batches (`Tracker::take_batch`) to the configured PostHog project.
#[derive(Clone)]
pub struct Sender {
    http: reqwest::Client,
    config: AnalyticsConfig,
}

impl Sender {
    pub fn new(config: AnalyticsConfig) -> Self {
        Self {
            http: reqwest::Client::new(),
            config,
        }
    }

    pub fn api_key(&self) -> &str {
        &self.config.api_key
    }

    /// Sends one batch body. Whether it arrived: failures, error statuses
    /// and timeouts are all just `false`; nothing here panics or retries,
    /// so analytics can't hold anything up.
    pub async fn send(&self, body: String) -> bool {
        let request = self
            .http
            .post(self.config.batch_url())
            .header("Content-Type", "application/json")
            .body(body)
            .send();
        match select(Box::pin(request), Delay::new(SEND_TIMEOUT)).await {
            Either::Left((Ok(response), _)) => response.status().is_success(),
            // Failed, or timed out (dropping the request cancels it).
            Either::Left((Err(_), _)) | Either::Right(_) => false,
        }
    }
}
