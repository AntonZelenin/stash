//! HTTP client for the Stash backend API: request/response models,
//! authentication transport, and API-specific errors.

mod client;
mod error;
mod models;
mod token_store;

pub use client::{ApiClient, MAX_FILES_PER_DUPLICATE_CHECK, MAX_SEARCH_NOTE_LENGTH};
pub use error::{ApiError, FieldError};
pub use models::{
    Collection, CurrentUser, DuplicateGroup, ItemCounts, ItemCreated, ItemKindCounts, ItemQuery,
    ItemSort, ItemTypeCounts, ItemUpdate, ListCollectionsResponse, ListItemsResponse,
    ListTagsResponse, ListedFile, ListedItem, ListedTag, NewUpload, PlaybackUrl, PresignedUpload,
    RegisterResponse, SavedYear, SearchResponse, Tag, TextItemType, TokenPair, UploadCandidate,
    UploadStarted, UploadType,
};
pub use token_store::TokenStore;
