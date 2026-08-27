use std::fmt;

/// API-level errors, categorized by what the caller can usefully do about
/// them. Call sites may match on the variant to show more specific copy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApiError {
    /// The request could not be sent, or no response came back.
    Network,
    /// 401: missing/invalid/expired credentials or token.
    Unauthorized,
    /// 409: the resource already exists (e.g. duplicate email).
    Conflict,
    /// 422: the request did not pass validation.
    Validation,
    /// Any other non-2xx response.
    Server,
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            ApiError::Network => "Could not reach the server",
            ApiError::Unauthorized => "Invalid credentials",
            ApiError::Conflict => "That email is already registered",
            ApiError::Validation => "Invalid request",
            ApiError::Server => "Something went wrong",
        };
        write!(f, "{message}")
    }
}

impl std::error::Error for ApiError {}
