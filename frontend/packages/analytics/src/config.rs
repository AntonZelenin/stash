/// Where events go: a PostHog project's API key and ingestion host.
///
/// Built from three settings (at build time, by each platform crate):
/// analytics is on only if `enabled` is exactly `"true"` and both the key
/// and the host are set. Anything else (unset, empty, `"false"`) is off,
/// so builds and tests send nothing unless analytics was turned on
/// explicitly.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnalyticsConfig {
    /// The project API key (`phc_...`): PostHog's public, write-only
    /// ingestion key, made to be shipped in clients.
    pub api_key: String,
    /// The ingestion host, e.g. `https://eu.i.posthog.com`, without a
    /// trailing slash.
    pub host: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigError {
    /// Not a project API key: PostHog's start with `phc_`. (A personal API
    /// key, `phx_`, is a secret and must never be compiled into a client.)
    ApiKey,
    /// Not an `https://` origin (`http://` only for localhost), or it has
    /// a path or a trailing slash.
    Host,
}

impl AnalyticsConfig {
    /// The configuration if analytics is on, None if it's off, or an
    /// error if it's on but the key or host is unusable.
    pub fn from_settings(
        enabled: Option<&str>,
        api_key: Option<&str>,
        host: Option<&str>,
    ) -> Result<Option<AnalyticsConfig>, ConfigError> {
        let api_key = api_key.map(str::trim).unwrap_or_default();
        let host = host.map(str::trim).unwrap_or_default();
        if enabled.map(str::trim) != Some("true") || api_key.is_empty() || host.is_empty() {
            return Ok(None);
        }
        if !api_key.starts_with("phc_") {
            return Err(ConfigError::ApiKey);
        }
        if !valid_host(host) {
            return Err(ConfigError::Host);
        }
        Ok(Some(AnalyticsConfig {
            api_key: api_key.to_string(),
            host: host.to_string(),
        }))
    }

    /// PostHog's batch capture endpoint.
    pub fn batch_url(&self) -> String {
        format!("{}/batch/", self.host)
    }
}

fn valid_host(host: &str) -> bool {
    let rest = if let Some(rest) = host.strip_prefix("https://") {
        rest
    } else if let Some(rest) = host.strip_prefix("http://") {
        let name = rest.split(':').next().unwrap_or_default();
        if name != "localhost" && name != "127.0.0.1" {
            return false;
        }
        rest
    } else {
        return false;
    };
    !rest.is_empty() && !rest.contains('/')
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: Option<&str> = Some("phc_abc123");
    const HOST: Option<&str> = Some("https://eu.i.posthog.com");

    #[test]
    fn off_unless_explicitly_enabled_and_configured() {
        assert_eq!(AnalyticsConfig::from_settings(None, KEY, HOST), Ok(None));
        assert_eq!(
            AnalyticsConfig::from_settings(Some("false"), KEY, HOST),
            Ok(None)
        );
        assert_eq!(
            AnalyticsConfig::from_settings(Some("1"), KEY, HOST),
            Ok(None)
        );
        assert_eq!(
            AnalyticsConfig::from_settings(Some("true"), None, HOST),
            Ok(None)
        );
        assert_eq!(
            AnalyticsConfig::from_settings(Some("true"), Some(""), HOST),
            Ok(None)
        );
        assert_eq!(
            AnalyticsConfig::from_settings(Some("true"), KEY, None),
            Ok(None)
        );
    }

    #[test]
    fn on_when_enabled_and_configured() {
        let config = AnalyticsConfig::from_settings(Some("true"), KEY, HOST)
            .unwrap()
            .unwrap();
        assert_eq!(config.batch_url(), "https://eu.i.posthog.com/batch/");
    }

    #[test]
    fn rejects_anything_but_a_project_key() {
        assert_eq!(
            AnalyticsConfig::from_settings(Some("true"), Some("phx_personal"), HOST),
            Err(ConfigError::ApiKey)
        );
    }

    #[test]
    fn rejects_hosts_that_are_not_origins() {
        for host in [
            "eu.i.posthog.com",
            "https://eu.i.posthog.com/",
            "https://eu.i.posthog.com/batch",
            "http://eu.i.posthog.com",
            "https://",
        ] {
            assert_eq!(
                AnalyticsConfig::from_settings(Some("true"), KEY, Some(host)),
                Err(ConfigError::Host),
                "{host}"
            );
        }
        assert!(
            AnalyticsConfig::from_settings(Some("true"), KEY, Some("http://localhost:8010"))
                .unwrap()
                .is_some()
        );
    }
}
