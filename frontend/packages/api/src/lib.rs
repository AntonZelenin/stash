//! HTTP client for the Stash backend API: request/response models,
//! authentication transport, and API-specific errors.

mod client;
mod error;
mod models;
mod token_store;

pub use client::ApiClient;
pub use error::{ApiError, FieldError};
pub use models::{
    ItemCounts, ItemCreated, ItemQuery, ItemTypeCounts, ItemUpdate, ListItemsResponse,
    ListTagsResponse, ListedFile, ListedItem, NewUpload, PresignedUpload, RegisterResponse,
    SearchResponse, Tag, TextItemType, TokenPair, UploadStarted, UploadType,
};
pub use token_store::TokenStore;
