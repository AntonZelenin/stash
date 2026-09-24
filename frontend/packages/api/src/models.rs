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

#[derive(Debug, Clone, Serialize)]
pub(crate) struct ChangePasswordRequest {
    pub current_password: String,
    pub new_password: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TokenPair {
    pub access_token: String,
    pub refresh_token: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct CreateTextItemRequest {
    pub text: String,
    /// Tag names for the new item (existing tags reused, missing created).
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ItemCreated {
    pub id: String,
    pub status: String,
}

/// A user's tag.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Tag {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ListTagsResponse {
    pub tags: Vec<Tag>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct AssignTagRequest {
    pub name: String,
}

/// Narrows listing and search: only items of `item_type` (the API's
/// `type`, e.g. `"image"`; None = any), carrying *all* of `tag_ids`, and
/// with `favorites_only`, only favorites.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ItemQuery {
    pub item_type: Option<String>,
    pub tag_ids: Vec<String>,
    pub favorites_only: bool,
}

/// Metadata of a `"file"` item's stored file.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ListedFile {
    /// As uploaded, e.g. "Quarterly report.pdf".
    pub filename: String,
    pub content_type: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ListedItem {
    pub id: String,
    pub r#type: String,
    pub status: String,
    pub created_at: String,
    pub text: Option<String>,
    /// Temporary, pre-signed — set only for `type == "image"`/`"file"`.
    /// Files open inline where that's safe and possible, under their
    /// original filename; otherwise they download.
    pub download_url: Option<String>,
    /// Temporary, pre-signed URL of a small WebP version, for display. Set
    /// for images once the backend has generated it; until then, use
    /// `download_url`. `default` keeps older API responses deserializable.
    #[serde(default)]
    pub thumbnail_url: Option<String>,
    /// Set for `type == "file"`.
    #[serde(default)]
    pub file: Option<ListedFile>,
    /// The item's tags, sorted by name.
    #[serde(default)]
    pub tags: Vec<Tag>,
    #[serde(default)]
    pub is_favorite: bool,
}

/// Changes to an item's editable content (`PATCH /items/{id}`); `None`
/// fields are left as they are.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ItemUpdate {
    /// A note's or link's whole text (its type is detected again by the
    /// server), or an image's or file's caption, where empty removes it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// Files only: the name it's shown and downloaded under.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filename: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct SearchRequest {
    pub query: String,
    pub limit: u32,
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub item_type: Option<String>,
    pub tag_ids: Vec<String>,
    pub favorite: bool,
}

/// Same item shape as `ListItemsResponse`, best match first; no paging.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SearchResponse {
    pub items: Vec<ListedItem>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ListItemsResponse {
    pub items: Vec<ListedItem>,
    pub next_cursor: Option<String>,
}
