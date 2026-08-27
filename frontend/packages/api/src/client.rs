use reqwest::{Method, RequestBuilder};

use crate::error::ApiError;
use crate::models::{
    CreateTextItemRequest, ItemCreated, LoginRequest, RefreshRequest, RegisterRequest,
    RegisterResponse, TokenPair,
};

#[derive(Clone)]
pub struct ApiClient {
    base_url: String,
    http: reqwest::Client,
}

impl ApiClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            http: reqwest::Client::new(),
        }
    }

    pub async fn register(
        &self,
        email: &str,
        password: &str,
    ) -> Result<RegisterResponse, ApiError> {
        let response = self
            .http
            .post(format!("{}/users", self.base_url))
            .json(&RegisterRequest {
                email: email.to_string(),
                password: password.to_string(),
            })
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            201 => response.json().await.map_err(|_| ApiError::Server),
            409 => Err(ApiError::Conflict),
            422 => Err(ApiError::Validation),
            _ => Err(ApiError::Server),
        }
    }

    pub async fn login(&self, email: &str, password: &str) -> Result<TokenPair, ApiError> {
        let response = self
            .http
            .post(format!("{}/login", self.base_url))
            .json(&LoginRequest {
                email: email.to_string(),
                password: password.to_string(),
            })
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            422 => Err(ApiError::Validation),
            _ => Err(ApiError::Server),
        }
    }

    pub async fn refresh(&self, refresh_token: &str) -> Result<TokenPair, ApiError> {
        let response = self
            .http
            .post(format!("{}/refresh", self.base_url))
            .json(&RefreshRequest {
                refresh_token: refresh_token.to_string(),
            })
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    /// A request builder for `{base_url}{path}` with the given access token
    /// attached as a Bearer credential, for calling protected endpoints.
    pub fn authenticated(&self, method: Method, path: &str, access_token: &str) -> RequestBuilder {
        self.http
            .request(method, format!("{}{}", self.base_url, path))
            .bearer_auth(access_token)
    }

    pub async fn create_text_item(
        &self,
        access_token: &str,
        text: &str,
    ) -> Result<ItemCreated, ApiError> {
        let response = self
            .authenticated(Method::POST, "/items/text", access_token)
            .json(&CreateTextItemRequest {
                text: text.to_string(),
            })
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            202 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            422 => Err(ApiError::Validation),
            _ => Err(ApiError::Server),
        }
    }
}
