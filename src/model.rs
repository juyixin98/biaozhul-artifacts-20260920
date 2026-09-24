//! OCI manifest/index data model.
//!
//! The JSON shapes follow:
//! - OCI Image Manifest  — <https://github.com/opencontainers/image-spec/blob/main/manifest.md>
//! - OCI Image Index     — <https://github.com/opencontainers/image-spec/blob/main/image-index.md>
//!
//! Raw bytes are retained separately by the store; these structs are the
//! parsed view used for traversal.

use serde::{Deserialize, Serialize};

/// Media type of an OCI image index.
pub const MEDIA_INDEX: &str = "application/vnd.oci.image.index.v1+json";
/// Media type of an OCI image manifest.
pub const MEDIA_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
/// Docker image index (Docker Registry v2, fat manifest).
pub const MEDIA_DOCKER_INDEX: &str = "application/vnd.docker.distribution.manifest.list.v2+json";
/// Docker image manifest v2.
pub const MEDIA_DOCKER_MANIFEST: &str = "application/vnd.docker.distribution.manifest.v2+json";

/// Platform attributes attached to an index child descriptor
/// (`descriptor.platform` / Docker `manifest.platform`).
#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Platform {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub architecture: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os_version: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub os_features: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub variant: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub features: Vec<String>,
}

/// A content descriptor.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Descriptor {
    pub media_type: Option<String>,
    pub digest: String,
    #[serde(default)]
    pub size: u64,
    #[serde(flatten)]
    pub rest: serde_json::Map<String, serde_json::Value>,
}

impl Descriptor {
    /// Platform object attached to the descriptor, if any.
    pub fn platform(&self) -> Option<Platform> {
        self.rest
            .get("platform")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
    }
}

/// An image index (`manifests` array).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Index {
    #[serde(default, rename = "schemaVersion")]
    pub schema_version: Option<u32>,
    #[serde(default, rename = "mediaType")]
    pub media_type: Option<String>,
    #[serde(default)]
    pub manifests: Vec<Descriptor>,
}

/// An image manifest (a leaf for selection purposes; its `layers` and
/// `config` are not traversed).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Manifest {
    #[serde(default, rename = "schemaVersion")]
    pub schema_version: Option<u32>,
    #[serde(default, rename = "mediaType")]
    pub media_type: Option<String>,
    #[serde(default)]
    pub config: Option<Descriptor>,
    #[serde(default)]
    pub layers: Vec<Descriptor>,
}

/// Structured kind of a stored JSON blob.
#[derive(Debug, Clone)]
pub enum BlobKind {
    Index(Index),
    Manifest(Manifest),
    Other,
}

impl BlobKind {
    /// Classify a blob from its declared media type, falling back to shape
    /// detection for blobs that omit `mediaType` (allowed by the spec).
    pub fn classify(media_type: Option<&str>, bytes: &[u8]) -> Result<BlobKind, String> {
        let value: serde_json::Value =
            serde_json::from_slice(bytes).map_err(|e| format!("blob is not valid JSON: {e}"))?;
        match media_type {
            Some(MEDIA_INDEX) | Some(MEDIA_DOCKER_INDEX) => {
                Ok(BlobKind::Index(parse_index(value)?))
            }
            Some(MEDIA_MANIFEST) | Some(MEDIA_DOCKER_MANIFEST) => {
                Ok(BlobKind::Manifest(parse_manifest(value)?))
            }
            Some(other) => match classify_by_shape(value)? {
                Some(kind) => Ok(kind),
                None => Err(format!("unsupported manifest mediaType {other:?}")),
            },
            None => Ok(classify_by_shape(value)?.unwrap_or(BlobKind::Other)),
        }
    }
}

fn classify_by_shape(value: serde_json::Value) -> Result<Option<BlobKind>, String> {
    let is_object_like = value.is_object();
    if !is_object_like {
        return Ok(None);
    }
    let obj = value.as_object().expect("checked above");
    if obj.contains_key("manifests") {
        Ok(Some(BlobKind::Index(parse_index(value)?)))
    } else if obj.contains_key("config") || obj.contains_key("layers") {
        Ok(Some(BlobKind::Manifest(parse_manifest(value)?)))
    } else {
        Ok(None)
    }
}

fn parse_index(value: serde_json::Value) -> Result<Index, String> {
    serde_json::from_value(value).map_err(|e| format!("invalid image index: {e}"))
}

fn parse_manifest(value: serde_json::Value) -> Result<Manifest, String> {
    serde_json::from_value(value).map_err(|e| format!("invalid image manifest: {e}"))
}

/// Read `mediaType` from a JSON blob, returning it only when it is one of
/// the four OCI/Docker manifest media types this service understands.
pub fn known_media_type(bytes: &[u8]) -> Option<&'static str> {
    let value: serde_json::Value = serde_json::from_slice(bytes).ok()?;
    match value.get("mediaType").and_then(|v| v.as_str()) {
        Some(MEDIA_INDEX) => Some(MEDIA_INDEX),
        Some(MEDIA_MANIFEST) => Some(MEDIA_MANIFEST),
        Some(MEDIA_DOCKER_INDEX) => Some(MEDIA_DOCKER_INDEX),
        Some(MEDIA_DOCKER_MANIFEST) => Some(MEDIA_DOCKER_MANIFEST),
        _ => None,
    }
}
