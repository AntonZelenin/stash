use std::collections::HashMap;

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
    /// See `TextItemType`; unset for the server's default (`text`).
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub item_type: Option<TextItemType>,
}

/// The type chosen for a note/link whose text mixes text and URLs. The
/// server ignores it for anything else: a bare URL is always a link, text
/// without URLs always a note.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum TextItemType {
    Text,
    Link,
}

impl TextItemType {
    /// From an item's API `type`; None for images and files.
    pub fn from_api(item_type: &str) -> Option<Self> {
        match item_type {
            "text" => Some(Self::Text),
            "link" => Some(Self::Link),
            _ => None,
        }
    }

    pub fn as_api(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Link => "link",
        }
    }
}

/// What an upload becomes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum UploadType {
    Image,
    File,
}

/// An image or file about to be uploaded (`POST /uploads`). The bytes go
/// straight to storage afterwards; this is only their description, which
/// the backend validates before authorizing the upload.
#[derive(Debug, Clone, Serialize)]
pub struct NewUpload {
    #[serde(rename = "type")]
    pub upload_type: UploadType,
    #[serde(rename = "filename")]
    pub file_name: String,
    /// Images: their type. Files: ignored, the backend decides.
    pub content_type: String,
    /// Exact size; the upload URL only accepts that many bytes.
    pub size_bytes: u64,
    /// Optional caption for the new item.
    #[serde(rename = "text", skip_serializing_if = "Option::is_none")]
    pub caption: Option<String>,
    /// Tag names for the new item.
    pub tags: Vec<String>,
}

/// Where and how to send an upload's bytes: a short-lived, pre-signed
/// object storage request, not the API.
#[derive(Debug, Clone, Deserialize)]
pub struct PresignedUpload {
    pub url: String,
    pub method: String,
    /// Sent exactly as given; they're part of the signature.
    pub headers: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct UploadStarted {
    /// Finalize with it; also the id of the item it becomes.
    pub upload_id: String,
    pub upload: PresignedUpload,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ItemCreated {
    pub id: String,
    pub status: String,
}

/// The signed-in user's account (`GET /users/me`).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct CurrentUser {
    pub id: String,
    pub email: String,
}

/// How many items the user has: of each type and each kind (every one
/// present), and favorites. Not narrowed by any filter.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
pub struct ItemCounts {
    pub types: ItemTypeCounts,
    /// `default` keeps older API responses deserializable.
    #[serde(default)]
    pub kinds: ItemKindCounts,
    pub favorites: u32,
}

/// Item counts per API item type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
pub struct ItemTypeCounts {
    pub text: u32,
    pub link: u32,
    pub image: u32,
    pub file: u32,
}

impl ItemTypeCounts {
    pub fn total(&self) -> u32 {
        self.text + self.link + self.image + self.file
    }
}

/// Image and file counts per API item kind (the `kind` filter's values).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
pub struct ItemKindCounts {
    pub image: u32,
    pub video: u32,
    pub audio: u32,
    pub document: u32,
    pub book: u32,
    pub other: u32,
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

/// A calendar year the user saved things in: the first and last time they
/// did (RFC 3339). Times rather than a year number, since the server
/// doesn't know the user's time zone.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SavedYear {
    pub first_saved_at: String,
    pub last_saved_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub(crate) struct SavedYearsResponse {
    pub years: Vec<SavedYear>,
}

/// Listing order (`GET /items`'s `sort`); search always ranks by
/// relevance. `Random` is a fresh shuffle each request, with no next page.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ItemSort {
    #[default]
    Newest,
    Oldest,
    Random,
}

impl ItemSort {
    pub fn api_value(self) -> &'static str {
        match self {
            ItemSort::Newest => "newest",
            ItemSort::Oldest => "oldest",
            ItemSort::Random => "random",
        }
    }
}

/// Narrows listing and search: only items of `item_type` (the API's
/// `type`, e.g. `"link"`; None = any) and of any of `kinds` (the API's
/// `kind` values, e.g. `"video"`; empty = any), carrying *all* of
/// `tag_ids`, and with
/// `favorites_only`, only favorites; `created_from` (inclusive) and
/// `created_before` (exclusive) bound when it was saved, as RFC 3339
/// timestamps with an offset.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ItemQuery {
    pub item_type: Option<String>,
    pub kinds: Vec<String>,
    pub tag_ids: Vec<String>,
    pub favorites_only: bool,
    pub created_from: Option<String>,
    pub created_before: Option<String>,
}

/// Metadata of a `"file"` item's stored file.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ListedFile {
    /// As uploaded, e.g. "Quarterly report.pdf".
    pub filename: String,
    pub content_type: String,
    pub size_bytes: u64,
    /// What it holds, decided by the server from its content type:
    /// `"video"`, `"audio"`, `"document"`, `"book"` or `"other"`. Empty
    /// from older API responses.
    #[serde(default)]
    pub kind: String,
}

impl ListedFile {
    /// Video or audio: shown like images, on a neutral card.
    pub fn is_media(&self) -> bool {
        matches!(self.kind.as_str(), "video" | "audio")
    }

    /// Played in the video viewer (whatever its format: whether the
    /// browser can play it is only known by trying).
    pub fn is_video(&self) -> bool {
        self.kind == "video"
    }
}

/// Where to play a video item from (`GET /items/{id}/video-url`): a fresh,
/// pre-signed URL that lasts a viewing session and supports seeking. Once
/// it has expired, playback fails; fetch a new one.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct VideoUrl {
    pub url: String,
    /// RFC 3339.
    pub expires_at: String,
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
    /// A note's or link's whole text (its type is resolved again by the
    /// server), or an image's or file's caption, where empty removes it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// Files only: the name it's shown and downloaded under.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filename: Option<String>,
    /// Notes and links only: see `TextItemType`. Unset keeps the current
    /// type for text mixing text and URLs.
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub item_type: Option<TextItemType>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct SearchRequest {
    pub query: String,
    pub limit: u32,
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    pub item_type: Option<String>,
    pub kinds: Vec<String>,
    pub tag_ids: Vec<String>,
    pub favorite: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_before: Option<String>,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn file(kind: &str) -> ListedFile {
        ListedFile {
            filename: "x".to_string(),
            content_type: "application/octet-stream".to_string(),
            size_bytes: 1,
            kind: kind.to_string(),
        }
    }

    #[test]
    fn only_video_and_audio_files_are_media() {
        assert!(file("video").is_media());
        assert!(file("audio").is_media());
        for kind in ["document", "book", "other", ""] {
            assert!(!file(kind).is_media(), "{kind:?}");
        }
    }

    #[test]
    fn only_video_files_open_in_the_video_viewer() {
        assert!(file("video").is_video());
        for kind in ["audio", "document", "book", "other", ""] {
            assert!(!file(kind).is_video(), "{kind:?}");
        }
    }

    #[test]
    fn listed_files_without_a_kind_still_parse() {
        let parsed: ListedFile = serde_json::from_str(
            r#"{"filename": "a.pdf", "content_type": "application/pdf", "size_bytes": 3}"#,
        )
        .unwrap();
        assert_eq!(parsed.kind, "");
        assert!(!parsed.is_media());
    }
}
