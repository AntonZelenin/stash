use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize)]
pub(crate) struct RegisterRequest {
    pub email: String,
    pub password: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RegisterResponse {
    pub id: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct LoginRequest {
    pub email: String,
    pub password: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct RefreshRequest {
    pub refresh_token: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TokenPair {
    pub access_token: String,
    pub refresh_token: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct CreateTextItemRequest {
    pub text: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ItemCreated {
    pub id: String,
    pub status: String,
}
