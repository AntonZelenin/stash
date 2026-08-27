use std::fmt;

/// A single validation failure from the backend, optionally tied to one
/// request field (e.g. "password"). `field` is `None` for an error that
/// isn't about any specific field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldError {
    pub field: Option<String>,
    pub message: String,
}

/// API-level errors, categorized by what the caller can usefully do about
/// them. Call sites may match on the variant to show more specific copy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApiError {
    /// The request could not be sent, or no response came back.
    Network,
    /// 401: missing/invalid/expired credentials or token.
    Unauthorized,
    /// 409: the resource already exists (e.g. duplicate email).
    Conflict,
    /// 422: the request did not pass validation, broken down per field
    /// where the backend told us which field it was about.
    Validation(Vec<FieldError>),
    /// Any other non-2xx response.
    Server,
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApiError::Network => write!(f, "Could not reach the server"),
            ApiError::Unauthorized => write!(f, "Invalid credentials"),
            ApiError::Conflict => write!(f, "That email is already registered"),
            ApiError::Validation(errors) if !errors.is_empty() => {
                let messages: Vec<&str> = errors.iter().map(|e| e.message.as_str()).collect();
                write!(f, "{}", messages.join(" "))
            }
            ApiError::Validation(_) => write!(f, "Invalid request"),
            ApiError::Server => write!(f, "Something went wrong"),
        }
    }
}

impl std::error::Error for ApiError {}
