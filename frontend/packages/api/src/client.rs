use reqwest::{Method, RequestBuilder, Response};
use serde::Deserialize;

use crate::error::{ApiError, FieldError};
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
            422 => Err(ApiError::Validation(
                parse_validation_errors(response).await,
            )),
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
            422 => Err(ApiError::Validation(
                parse_validation_errors(response).await,
            )),
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
            422 => Err(ApiError::Validation(
                parse_validation_errors(response).await,
            )),
            _ => Err(ApiError::Server),
        }
    }
}

/// FastAPI's shape for a 422 from request-body validation:
/// `{"detail": [{"loc": ["body", "password"], "msg": "...", "type": "..."}]}`.
#[derive(Deserialize)]
struct ValidationErrorBody {
    detail: Vec<ValidationErrorItem>,
}

#[derive(Deserialize)]
struct ValidationErrorItem {
    loc: Vec<serde_json::Value>,
    msg: String,
}

/// Parses a 422 response into field-level errors. Falls back to a single
/// generic, field-less error if the body doesn't match the shape above (e.g.
/// a 422 raised manually with a plain string `detail`).
async fn parse_validation_errors(response: Response) -> Vec<FieldError> {
    match response.json::<ValidationErrorBody>().await {
        Ok(body) => body
            .detail
            .into_iter()
            .map(|item| FieldError {
                // `loc` is `["body", "<field name>", ...]` for a body field;
                // take the field name directly under "body" if present.
                field: item.loc.get(1).and_then(|v| v.as_str()).map(str::to_string),
                message: item.msg,
            })
            .collect(),
        Err(_) => vec![FieldError {
            field: None,
            message: "Invalid request".to_string(),
        }],
    }
}
