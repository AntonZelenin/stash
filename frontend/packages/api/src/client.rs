use reqwest::{Method, RequestBuilder, Response};
use serde::Deserialize;

use crate::error::{ApiError, FieldError};
use crate::models::{
    AssignTagRequest, CreateTextItemRequest, ItemCreated, ItemQuery, ListItemsResponse,
    ListTagsResponse, LoginRequest, RefreshRequest, RegisterRequest, RegisterResponse,
    SearchRequest, SearchResponse, Tag, TokenPair,
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

    /// `tags`: names of tags to put on the new item.
    pub async fn create_text_item(
        &self,
        access_token: &str,
        text: &str,
        tags: &[String],
    ) -> Result<ItemCreated, ApiError> {
        let response = self
            .authenticated(Method::POST, "/items/text", access_token)
            .json(&CreateTextItemRequest {
                text: text.to_string(),
                tags: tags.to_vec(),
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

    /// Permanently deletes an item. A 404 counts as success: either way the
    /// item is gone, which is all the caller asked for (e.g. it was already
    /// deleted from another tab).
    /// The user's tags containing `query` (all of them if empty), names
    /// starting with it first.
    pub async fn list_tags(
        &self,
        access_token: &str,
        query: &str,
        limit: u32,
    ) -> Result<ListTagsResponse, ApiError> {
        let response = self
            .authenticated(Method::GET, "/tags", access_token)
            .query(&[("query", query.to_string()), ("limit", limit.to_string())])
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    /// Tags the item with `name`, reusing the user's existing tag of that
    /// name (ignoring case) or creating it. Returns the tag.
    pub async fn assign_tag(
        &self,
        access_token: &str,
        item_id: &str,
        name: &str,
    ) -> Result<Tag, ApiError> {
        let response = self
            .authenticated(
                Method::POST,
                &format!("/items/{item_id}/tags"),
                access_token,
            )
            .json(&AssignTagRequest {
                name: name.to_string(),
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

    pub async fn remove_tag(
        &self,
        access_token: &str,
        item_id: &str,
        tag_id: &str,
    ) -> Result<(), ApiError> {
        let response = self
            .authenticated(
                Method::DELETE,
                &format!("/items/{item_id}/tags/{tag_id}"),
                access_token,
            )
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            204 => Ok(()),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    pub async fn delete_item(&self, access_token: &str, item_id: &str) -> Result<(), ApiError> {
        let response = self
            .authenticated(Method::DELETE, &format!("/items/{item_id}"), access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            204 | 404 => Ok(()),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    /// Semantic search over the user's items, most similar first,
    /// narrowed by `filters`.
    pub async fn search_items(
        &self,
        access_token: &str,
        query: &str,
        limit: u32,
        filters: &ItemQuery,
    ) -> Result<SearchResponse, ApiError> {
        let response = self
            .authenticated(Method::POST, "/search", access_token)
            .json(&SearchRequest {
                query: query.to_string(),
                limit,
                item_type: filters.item_type.clone(),
                tag_ids: filters.tag_ids.clone(),
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

    pub async fn create_image_item(
        &self,
        access_token: &str,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        text: Option<&str>,
        tags: &[String],
    ) -> Result<ItemCreated, ApiError> {
        self.upload_file(
            "/items/image",
            access_token,
            file_name,
            content_type,
            data,
            ItemFields { text, tags },
        )
        .await
    }

    /// Uploads any file (PDF, e-book, archive, anything). The backend decides the
    /// type from the file name's extension and checks the content matches.
    pub async fn create_file_item(
        &self,
        access_token: &str,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        text: Option<&str>,
        tags: &[String],
    ) -> Result<ItemCreated, ApiError> {
        self.upload_file(
            "/items/file",
            access_token,
            file_name,
            content_type,
            data,
            ItemFields { text, tags },
        )
        .await
    }

    /// Shared multipart upload: the file as `file`, plus an optional
    /// caption as `text`, stored on the same item.
    async fn upload_file(
        &self,
        path: &str,
        access_token: &str,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        fields: ItemFields<'_>,
    ) -> Result<ItemCreated, ApiError> {
        let ItemFields { text, tags } = fields;
        let part = reqwest::multipart::Part::bytes(data)
            .file_name(file_name.to_string())
            .mime_str(content_type)
            .map_err(|_| ApiError::Server)?;
        let mut form = reqwest::multipart::Form::new().part("file", part);
        if let Some(text) = text {
            form = form.text("text", text.to_string());
        }
        // One `tags` field per tag name.
        for tag in tags {
            form = form.text("tags", tag.clone());
        }

        let response = self
            .authenticated(Method::POST, path, access_token)
            .multipart(form)
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

    pub async fn list_items(
        &self,
        access_token: &str,
        cursor: Option<&str>,
        limit: u32,
        filters: &ItemQuery,
    ) -> Result<ListItemsResponse, ApiError> {
        let mut params: Vec<(&str, String)> = vec![("limit", limit.to_string())];
        if let Some(cursor) = cursor {
            params.push(("cursor", cursor.to_string()));
        }
        if let Some(item_type) = &filters.item_type {
            params.push(("type", item_type.clone()));
        }
        for tag_id in &filters.tag_ids {
            params.push(("tag_id", tag_id.clone()));
        }

        let response = self
            .authenticated(Method::GET, "/items", access_token)
            .query(&params)
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
}

/// The optional fields sent along with an uploaded file: its caption and
/// tag names.
struct ItemFields<'a> {
    text: Option<&'a str>,
    tags: &'a [String],
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

/// FastAPI's shape for a 422 raised manually via `HTTPException(422, "...")`
/// — e.g. "File is too large", "Unsupported image type", "Invalid cursor".
/// Every message on this path is a short, deliberately user-facing string
/// the backend chose to explain what was wrong with the request — never
/// exception internals — so unlike a 500 (always the generic message, body
/// ignored entirely) it's safe to show as-is.
#[derive(Deserialize)]
struct StringDetailBody {
    detail: String,
}

fn generic_validation_error() -> FieldError {
    FieldError {
        field: None,
        message: "Invalid request".to_string(),
    }
}

/// Parses a 422 response into field-level errors, trying both shapes the
/// backend can send. Falls back to a single generic, field-less error only
/// if the body matches neither (or isn't readable at all).
async fn parse_validation_errors(response: Response) -> Vec<FieldError> {
    let Ok(text) = response.text().await else {
        return vec![generic_validation_error()];
    };

    if let Ok(body) = serde_json::from_str::<ValidationErrorBody>(&text) {
        return body
            .detail
            .into_iter()
            .map(|item| FieldError {
                // `loc` is `["body", "<field name>", ...]` for a body field;
                // take the field name directly under "body" if present.
                field: item.loc.get(1).and_then(|v| v.as_str()).map(str::to_string),
                message: item.msg,
            })
            .collect();
    }

    if let Ok(body) = serde_json::from_str::<StringDetailBody>(&text) {
        return vec![FieldError {
            field: None,
            message: body.detail,
        }];
    }

    vec![generic_validation_error()]
}
