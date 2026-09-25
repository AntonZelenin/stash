use std::future::Future;
use std::sync::Arc;

use api::{
    ApiClient, ApiError, ItemCreated, ItemQuery, ItemUpdate, ListItemsResponse, ListedItem,
    NewUpload, SearchResponse, Tag, TokenPair, TokenStore, UploadType,
};
use dioxus::prelude::*;

/// Shared, reactive auth state. A platform entrypoint constructs one (with
/// its own `TokenStore` implementation) and provides it via
/// `use_context_provider`; components read/act on it via `use_context`.
#[derive(Clone)]
pub struct AuthSession {
    client: ApiClient,
    store: Arc<dyn TokenStore>,
    pub tokens: Signal<Option<TokenPair>>,
}

// Required for use as a #[component] prop. The client/store are fixed for a
// session's lifetime, so equality of the reactive `tokens` signal is what
// actually determines whether two handles represent the same live session.
impl PartialEq for AuthSession {
    fn eq(&self, other: &Self) -> bool {
        self.tokens == other.tokens
    }
}

impl AuthSession {
    pub fn new(client: ApiClient, store: Arc<dyn TokenStore>) -> Self {
        let tokens = Signal::new(store.load());
        Self {
            client,
            store,
            tokens,
        }
    }

    pub fn is_authenticated(&self) -> bool {
        self.tokens.read().is_some()
    }

    pub async fn login(&self, email: &str, password: &str) -> Result<(), ApiError> {
        let tokens = self.client.login(email, password).await?;
        self.store.save(&tokens);
        // Signal::set needs `&mut Signal`; copy the (Copy) handle out of the
        // `&self` field first — all copies alias the same reactive slot.
        let mut slot = self.tokens;
        slot.set(Some(tokens));
        Ok(())
    }

    /// Registers the account, then immediately logs it in.
    pub async fn register(&self, email: &str, password: &str) -> Result<(), ApiError> {
        self.client.register(email, password).await?;
        self.login(email, password).await
    }

    pub fn logout(&self) {
        self.store.clear();
        let mut slot = self.tokens;
        slot.set(None);
    }

    /// Changes the password and switches to the new token pair the server
    /// returns (it ends every other session, this one's old tokens
    /// included).
    pub async fn change_password(
        &self,
        current_password: &str,
        new_password: &str,
    ) -> Result<(), ApiError> {
        let client = self.client.clone();
        let current_password = current_password.to_string();
        let new_password = new_password.to_string();
        let tokens = self
            .call_authenticated(move |access_token| {
                let client = client.clone();
                let current_password = current_password.clone();
                let new_password = new_password.clone();
                async move {
                    client
                        .change_password(&access_token, &current_password, &new_password)
                        .await
                }
            })
            .await?;
        self.store.save(&tokens);
        let mut slot = self.tokens;
        slot.set(Some(tokens));
        Ok(())
    }

    pub async fn create_text_item(
        &self,
        text: &str,
        tags: Vec<String>,
    ) -> Result<ItemCreated, ApiError> {
        let client = self.client.clone();
        let text = text.to_string();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let text = text.clone();
            let tags = tags.clone();
            async move { client.create_text_item(&access_token, &text, &tags).await }
        })
        .await
    }

    pub async fn create_image_item(
        &self,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        text: Option<String>,
        tags: Vec<String>,
    ) -> Result<ItemCreated, ApiError> {
        self.upload_item(UploadType::Image, file_name, content_type, data, text, tags)
            .await
    }

    /// Uploads any file (PDF, e-book, archive, anything). The backend decides the
    /// type from the file name's extension and checks the content matches.
    pub async fn create_file_item(
        &self,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        text: Option<String>,
        tags: Vec<String>,
    ) -> Result<ItemCreated, ApiError> {
        self.upload_item(UploadType::File, file_name, content_type, data, text, tags)
            .await
    }

    /// Saves an image or file: the API authorizes the upload, the bytes go
    /// straight to storage, then the API creates the item. Only the two API
    /// calls are retried after a token refresh; the bytes are sent once.
    async fn upload_item(
        &self,
        upload_type: UploadType,
        file_name: &str,
        content_type: &str,
        data: Vec<u8>,
        caption: Option<String>,
        tags: Vec<String>,
    ) -> Result<ItemCreated, ApiError> {
        let upload = NewUpload {
            upload_type,
            file_name: file_name.to_string(),
            content_type: content_type.to_string(),
            size_bytes: data.len() as u64,
            caption,
            tags,
        };
        let client = self.client.clone();
        let started = self
            .call_authenticated(move |access_token| {
                let client = client.clone();
                let upload = upload.clone();
                async move { client.start_upload(&access_token, &upload).await }
            })
            .await?;

        self.client.upload_to_storage(&started.upload, data).await?;

        let client = self.client.clone();
        let upload_id = started.upload_id;
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let upload_id = upload_id.clone();
            async move { client.finalize_upload(&access_token, &upload_id).await }
        })
        .await
    }

    pub async fn list_items(
        &self,
        cursor: Option<String>,
        limit: u32,
        filters: ItemQuery,
    ) -> Result<ListItemsResponse, ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let cursor = cursor.clone();
            let filters = filters.clone();
            async move {
                client
                    .list_items(&access_token, cursor.as_deref(), limit, &filters)
                    .await
            }
        })
        .await
    }

    pub async fn delete_item(&self, item_id: String) -> Result<(), ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let item_id = item_id.clone();
            async move { client.delete_item(&access_token, &item_id).await }
        })
        .await
    }

    pub async fn search_items(
        &self,
        query: String,
        limit: u32,
        filters: ItemQuery,
    ) -> Result<SearchResponse, ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let query = query.clone();
            let filters = filters.clone();
            async move {
                client
                    .search_items(&access_token, &query, limit, &filters)
                    .await
            }
        })
        .await
    }

    pub async fn list_tags(&self, query: String, limit: u32) -> Result<Vec<Tag>, ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let query = query.clone();
            async move {
                client
                    .list_tags(&access_token, &query, limit)
                    .await
                    .map(|response| response.tags)
            }
        })
        .await
    }

    pub async fn assign_tag(&self, item_id: String, name: String) -> Result<Tag, ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let item_id = item_id.clone();
            let name = name.clone();
            async move { client.assign_tag(&access_token, &item_id, &name).await }
        })
        .await
    }

    pub async fn update_item(
        &self,
        item_id: String,
        update: ItemUpdate,
    ) -> Result<ListedItem, ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let item_id = item_id.clone();
            let update = update.clone();
            async move { client.update_item(&access_token, &item_id, &update).await }
        })
        .await
    }

    pub async fn set_favorite(&self, item_id: String, favorite: bool) -> Result<(), ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let item_id = item_id.clone();
            async move { client.set_favorite(&access_token, &item_id, favorite).await }
        })
        .await
    }

    pub async fn remove_tag(&self, item_id: String, tag_id: String) -> Result<(), ApiError> {
        let client = self.client.clone();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let item_id = item_id.clone();
            let tag_id = tag_id.clone();
            async move { client.remove_tag(&access_token, &item_id, &tag_id).await }
        })
        .await
    }

    /// Runs an authenticated call with the current access token. If the
    /// server reports it as expired/invalid (401), transparently redeems
    /// the stored refresh token for a new pair and retries once. If the
    /// refresh token itself is no longer valid, the session is logged out
    /// (which sends the user back to the login screen).
    async fn call_authenticated<T, F, Fut>(&self, call: F) -> Result<T, ApiError>
    where
        F: Fn(String) -> Fut,
        Fut: Future<Output = Result<T, ApiError>>,
    {
        let access_token = self.access_token()?;

        match call(access_token).await {
            Err(ApiError::Unauthorized) => {
                self.refresh().await?;
                call(self.access_token()?).await
            }
            result => result,
        }
    }

    fn access_token(&self) -> Result<String, ApiError> {
        self.tokens
            .read()
            .as_ref()
            .map(|tokens| tokens.access_token.clone())
            .ok_or(ApiError::Unauthorized)
    }

    /// Exchanges the stored refresh token for a new pair. Logs the session
    /// out if the refresh token has expired or was already used.
    async fn refresh(&self) -> Result<(), ApiError> {
        let Some(refresh_token) = self.tokens.read().as_ref().map(|t| t.refresh_token.clone())
        else {
            return Err(ApiError::Unauthorized);
        };

        match self.client.refresh(&refresh_token).await {
            Ok(new_tokens) => {
                self.store.save(&new_tokens);
                let mut slot = self.tokens;
                slot.set(Some(new_tokens));
                Ok(())
            }
            Err(err) => {
                self.logout();
                Err(err)
            }
        }
    }
}
