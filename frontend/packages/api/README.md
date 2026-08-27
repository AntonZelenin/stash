# API

HTTP client for the Stash backend API.

Contains:
- `ApiClient` — thin `reqwest`-based wrapper for the backend's HTTP endpoints (registration, login, token refresh, and an `authenticated()` helper for calling protected endpoints with a bearer token).
- Request/response models (`RegisterResponse`, `TokenPair`, ...).
- `ApiError` — a small set of error categories (`Network`, `Unauthorized`, `Conflict`, `Validation`, `Server`) so callers can react to *why* a request failed.
- `TokenStore` — the storage contract for persisting the current token pair. Storage is inherently platform-specific (browser `localStorage`, a native keychain, ...), so this crate only defines the trait; each platform crate that needs auth (currently `web`) provides its own implementation.

This crate has no Dioxus dependency and no UI code — it should stay usable from any platform crate.
