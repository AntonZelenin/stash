use reqwest::{Method, RequestBuilder, Response};
use serde::Deserialize;

use crate::error::{ApiError, FieldError};
use crate::models::{
    AssignTagRequest, ChangePasswordRequest, CreateTextItemRequest, CurrentUser, ItemCounts,
    ItemCreated, ItemQuery, ItemSort, ItemUpdate, ListItemsResponse, ListTagsResponse, ListedItem,
    LoginRequest, NewUpload, PresignedUpload, RefreshRequest, RegisterRequest, RegisterResponse,
    SavedYear, SavedYearsResponse, SearchRequest, SearchResponse, Tag, TextItemType, TokenPair,
    UploadStarted,
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

    /// Changes the password. Every existing session ends, including the
    /// one `access_token` belongs to; the returned pair replaces it. A
    /// wrong current password is a validation error on `current_password`.
    pub async fn change_password(
        &self,
        access_token: &str,
        current_password: &str,
        new_password: &str,
    ) -> Result<TokenPair, ApiError> {
        let response = self
            .authenticated(Method::POST, "/users/me/password", access_token)
            .json(&ChangePasswordRequest {
                current_password: current_password.to_string(),
                new_password: new_password.to_string(),
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
        item_type: Option<TextItemType>,
    ) -> Result<ItemCreated, ApiError> {
        let response = self
            .authenticated(Method::POST, "/items/text", access_token)
            .json(&CreateTextItemRequest {
                text: text.to_string(),
                tags: tags.to_vec(),
                item_type,
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

    /// Up to `limit` of the user's tags to offer when adding one: recently
    /// used first, then frequently used. With `item_id`, tags already on
    /// that item are left out. Empty if the user has no tags.
    pub async fn suggest_tags(
        &self,
        access_token: &str,
        item_id: Option<&str>,
        limit: u32,
    ) -> Result<ListTagsResponse, ApiError> {
        let mut params = vec![("limit", limit.to_string())];
        if let Some(item_id) = item_id {
            params.push(("item_id", item_id.to_string()));
        }
        let response = self
            .authenticated(Method::GET, "/tags/suggestions", access_token)
            .query(&params)
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

    /// Marks (`favorite`) or unmarks the item as a favorite. Idempotent.
    pub async fn set_favorite(
        &self,
        access_token: &str,
        item_id: &str,
        favorite: bool,
    ) -> Result<(), ApiError> {
        let method = if favorite {
            Method::PUT
        } else {
            Method::DELETE
        };
        let response = self
            .authenticated(method, &format!("/items/{item_id}/favorite"), access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            204 => Ok(()),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    /// Edits the item in place and returns it as updated (same id; a
    /// note or link may come back with a different `type`).
    pub async fn update_item(
        &self,
        access_token: &str,
        item_id: &str,
        update: &ItemUpdate,
    ) -> Result<ListedItem, ApiError> {
        let response = self
            .authenticated(Method::PATCH, &format!("/items/{item_id}"), access_token)
            .json(update)
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

    /// The item as it is now; None if it no longer exists.
    pub async fn get_item(
        &self,
        access_token: &str,
        item_id: &str,
    ) -> Result<Option<ListedItem>, ApiError> {
        self.get_optional_item(&format!("/items/{item_id}"), access_token)
            .await
    }

    /// One of the user's items, picked at random; None if they have none.
    pub async fn random_item(&self, access_token: &str) -> Result<Option<ListedItem>, ApiError> {
        self.get_optional_item("/items/random", access_token).await
    }

    async fn get_optional_item(
        &self,
        path: &str,
        access_token: &str,
    ) -> Result<Option<ListedItem>, ApiError> {
        let response = self
            .authenticated(Method::GET, path, access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response
                .json()
                .await
                .map(Some)
                .map_err(|_| ApiError::Server),
            404 => Ok(None),
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
                kinds: filters.kinds.clone(),
                tag_ids: filters.tag_ids.clone(),
                favorite: filters.favorites_only,
                created_from: filters.created_from.clone(),
                created_before: filters.created_before.clone(),
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

    /// Step 1 of saving an image or file: asks the API to authorize an
    /// upload. The bytes are then sent straight to storage
    /// (`upload_to_storage`), never through the API, and the item is
    /// created by `finalize_upload`.
    pub async fn start_upload(
        &self,
        access_token: &str,
        upload: &NewUpload,
    ) -> Result<UploadStarted, ApiError> {
        let response = self
            .authenticated(Method::POST, "/uploads", access_token)
            .json(upload)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            201 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            422 => Err(ApiError::Validation(
                parse_validation_errors(response).await,
            )),
            _ => Err(ApiError::Server),
        }
    }

    /// Step 2: sends the file's bytes to object storage with the
    /// pre-signed request from `start_upload`. Unauthenticated: the URL's
    /// signature is the authorization, so no API token is sent there.
    pub async fn upload_to_storage(
        &self,
        upload: &PresignedUpload,
        data: Vec<u8>,
    ) -> Result<(), ApiError> {
        let method = Method::from_bytes(upload.method.as_bytes()).map_err(|_| ApiError::Server)?;
        let mut request = self.http.request(method, &upload.url).body(data);
        for (name, value) in &upload.headers {
            request = request.header(name, value);
        }
        let response = request.send().await.map_err(|_| ApiError::Network)?;

        if response.status().is_success() {
            Ok(())
        } else {
            // Storage answers in its own (XML) format; an expired URL or a
            // body that doesn't match what was signed ends up here.
            Err(ApiError::Server)
        }
    }

    /// Step 3: creates the item from the finished upload. Safe to retry: an
    /// upload that already became an item returns that item.
    pub async fn finalize_upload(
        &self,
        access_token: &str,
        upload_id: &str,
    ) -> Result<ItemCreated, ApiError> {
        let response = self
            .authenticated(
                Method::POST,
                &format!("/uploads/{upload_id}/finalize"),
                access_token,
            )
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            202 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            // Upload not found (404) or not in storage (409): the backend's
            // message says which, as for invalid content (422).
            404 | 409 | 422 => Err(ApiError::Validation(
                parse_validation_errors(response).await,
            )),
            _ => Err(ApiError::Server),
        }
    }

    /// How many items the user has of each type, and how many are
    /// favorites.
    pub async fn current_user(&self, access_token: &str) -> Result<CurrentUser, ApiError> {
        let response = self
            .authenticated(Method::GET, "/users/me", access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    pub async fn count_items(&self, access_token: &str) -> Result<ItemCounts, ApiError> {
        let response = self
            .authenticated(Method::GET, "/items/counts", access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response.json().await.map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    /// One entry per calendar year the user saved things in, oldest first.
    pub async fn saved_years(&self, access_token: &str) -> Result<Vec<SavedYear>, ApiError> {
        let response = self
            .authenticated(Method::GET, "/items/years", access_token)
            .send()
            .await
            .map_err(|_| ApiError::Network)?;

        match response.status().as_u16() {
            200 => response
                .json::<SavedYearsResponse>()
                .await
                .map(|body| body.years)
                .map_err(|_| ApiError::Server),
            401 => Err(ApiError::Unauthorized),
            _ => Err(ApiError::Server),
        }
    }

    pub async fn list_items(
        &self,
        access_token: &str,
        cursor: Option<&str>,
        limit: u32,
        filters: &ItemQuery,
        sort: ItemSort,
    ) -> Result<ListItemsResponse, ApiError> {
        let mut params: Vec<(&str, String)> = vec![
            ("limit", limit.to_string()),
            ("sort", sort.api_value().to_string()),
        ];
        if let Some(cursor) = cursor {
            params.push(("cursor", cursor.to_string()));
        }
        if let Some(item_type) = &filters.item_type {
            params.push(("type", item_type.clone()));
        }
        for kind in &filters.kinds {
            params.push(("kind", kind.clone()));
        }
        for tag_id in &filters.tag_ids {
            params.push(("tag_id", tag_id.clone()));
        }
        if filters.favorites_only {
            params.push(("favorite", "true".to_string()));
        }
        if let Some(created_from) = &filters.created_from {
            params.push(("created_from", created_from.clone()));
        }
        if let Some(created_before) = &filters.created_before {
            params.push(("created_before", created_before.clone()));
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
