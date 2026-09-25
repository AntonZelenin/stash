//! Build-time configuration of the web app.
//!
//! A WASM app has no process environment at runtime, so settings are read
//! from the environment when the app is compiled and baked into the binary.
//! This is the only place that reads them.
//!
//! - `STASH_API_BASE_URL`: the backend API's base URL, without a trailing
//!   slash, e.g. `https://abc123.execute-api.eu-central-1.amazonaws.com`.
//!   Required for release builds (`dx build --release`), which fail to
//!   compile without it. Debug builds (`dx serve`) default to
//!   `DEV_API_BASE_URL`, the local docker-compose API.
//!
//! Cargo rebuilds the crate when the variable changes.

/// The backend API's base URL.
pub const API_BASE_URL: &str = api_base_url();

/// The API published by the local dev/docker-compose setup
/// (`docker-compose.yml`'s `API_PORT`, default 8000).
const DEV_API_BASE_URL: &str = "http://localhost:8000";

const fn api_base_url() -> &'static str {
    let url = match option_env!("STASH_API_BASE_URL") {
        Some(url) => url,
        None if cfg!(debug_assertions) => DEV_API_BASE_URL,
        None => panic!(
            "STASH_API_BASE_URL is not set: release builds need the backend API's base URL, \
             e.g. STASH_API_BASE_URL=https://api.example.com dx build --release"
        ),
    };
    // Evaluated at compile time, so an invalid value fails the build.
    match validate_base_url(url) {
        Ok(()) => url,
        Err(BaseUrlError::Empty) => panic!("STASH_API_BASE_URL is empty"),
        Err(BaseUrlError::Scheme) => {
            panic!("STASH_API_BASE_URL must start with http:// or https://")
        }
        Err(BaseUrlError::TrailingSlash) => {
            panic!("STASH_API_BASE_URL must not end with a slash")
        }
    }
}

#[derive(Debug, PartialEq)]
enum BaseUrlError {
    Empty,
    Scheme,
    TrailingSlash,
}

/// Checks the shape `ApiClient` relies on: an absolute http(s) URL that
/// request paths (`/items`, ...) are appended to as-is.
const fn validate_base_url(url: &str) -> Result<(), BaseUrlError> {
    let bytes = url.as_bytes();
    if bytes.is_empty() {
        return Err(BaseUrlError::Empty);
    }
    if !starts_with(bytes, b"https://") && !starts_with(bytes, b"http://") {
        return Err(BaseUrlError::Scheme);
    }
    if bytes[bytes.len() - 1] == b'/' {
        return Err(BaseUrlError::TrailingSlash);
    }
    Ok(())
}

const fn starts_with(bytes: &[u8], prefix: &[u8]) -> bool {
    if bytes.len() <= prefix.len() {
        return false;
    }
    let mut i = 0;
    while i < prefix.len() {
        if bytes[i] != prefix[i] {
            return false;
        }
        i += 1;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_http_and_https_urls() {
        assert_eq!(validate_base_url("http://localhost:8000"), Ok(()));
        assert_eq!(
            validate_base_url("https://abc123.execute-api.eu-central-1.amazonaws.com"),
            Ok(())
        );
        assert_eq!(validate_base_url("https://api.example.com/v1"), Ok(()));
    }

    #[test]
    fn rejects_empty() {
        assert_eq!(validate_base_url(""), Err(BaseUrlError::Empty));
    }

    #[test]
    fn rejects_missing_or_other_scheme() {
        assert_eq!(
            validate_base_url("localhost:8000"),
            Err(BaseUrlError::Scheme)
        );
        assert_eq!(
            validate_base_url("ftp://example.com"),
            Err(BaseUrlError::Scheme)
        );
        assert_eq!(validate_base_url("https://"), Err(BaseUrlError::Scheme));
    }

    #[test]
    fn rejects_trailing_slash() {
        assert_eq!(
            validate_base_url("https://api.example.com/"),
            Err(BaseUrlError::TrailingSlash)
        );
    }

    #[test]
    fn dev_default_is_valid() {
        assert_eq!(validate_base_url(DEV_API_BASE_URL), Ok(()));
    }
}
