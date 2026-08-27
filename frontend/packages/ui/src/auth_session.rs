use std::future::Future;
use std::sync::Arc;

use api::{ApiClient, ApiError, ItemCreated, TokenPair, TokenStore};
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

    pub async fn create_text_item(&self, text: &str) -> Result<ItemCreated, ApiError> {
        let client = self.client.clone();
        let text = text.to_string();
        self.call_authenticated(move |access_token| {
            let client = client.clone();
            let text = text.clone();
            async move { client.create_text_item(&access_token, &text).await }
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
