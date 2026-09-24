//! OCI image-spec data types used by the service.
//!
//! Only the subset needed for multi-arch selection is modelled: image
//! manifests (`application/vnd.oci.image.manifest.v1+json`) and image
//! indexes (`application/vnd.oci.image.index.v1+json`). Fields not needed
//! for selection are still accepted (unknown fields are ignored by serde).

use serde::{Deserialize, Serialize};

/// Manifest bytes could not be parsed as a supported OCI manifest.
/// Raised before any digest context exists; callers map it to
/// [`crate::selector::SelectError::InvalidManifest`].
#[derive(Debug, Clone, thiserror::Error)]
#[error("{0}")]
pub struct ManifestParseError(pub String);

impl ManifestParseError {
    pub fn code(&self) -> &'static str {
        "INVALID_MANIFEST"
    }
}

/// OCI content descriptor — a pointer to a blob plus optional platform.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Descriptor {
    #[serde(rename = "mediaType", default, skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,

    /// Content digest the blob must have. Verified during selection.
    pub digest: String,

    /// Content size in bytes the blob must have. Verified during selection.
    pub size: i64,

    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub urls: Vec<String>,

    /// Present on entries of a multi-platform index.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub platform: Option<Platform>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub annotations: Option<serde_json::Map<String, serde_json::Value>>,
}

impl Descriptor {
    /// Best-effort classification used only to decide whether an index
    /// entry can itself be traversed as an index. Unknown media types are
    /// treated conservatively (as image manifests).
    pub fn looks_like_index(&self) -> bool {
        matches!(
            self.media_type.as_deref(),
            Some("application/vnd.oci.image.index.v1+json")
                | Some("application/vnd.docker.distribution.manifest.list.v2+json")
        )
    }
}

/// The `platform` object of an OCI descriptor.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Platform {
    pub architecture: String,
    pub os: String,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub variant: Option<String>,

    #[serde(rename = "os.version", default, skip_serializing_if = "Option::is_none")]
    pub os_version: Option<String>,

    #[serde(rename = "os.features", default, skip_serializing_if = "Vec::is_empty")]
    pub os_features: Vec<String>,

    /// CPU feature flags (e.g. `sse4_2`).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub features: Vec<String>,
}

/// Standard image manifest fields needed for the response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageManifest {
    #[serde(rename = "schemaVersion", default)]
    pub schema_version: i64,
    #[serde(rename = "mediaType", default, skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
    pub config: Descriptor,
    #[serde(default)]
    pub layers: Vec<Descriptor>,
}

/// Image index (manifest list) fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexManifest {
    #[serde(rename = "schemaVersion", default)]
    pub schema_version: i64,
    #[serde(rename = "mediaType", default, skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
    #[serde(default)]
    pub manifests: Vec<Descriptor>,
}

/// A parsed manifest: either a leaf image manifest or an index.
#[derive(Debug, Clone)]
pub struct Manifest {
    pub schema_version: i64,
    pub media_type: Option<String>,
    pub kind: ManifestKind,
    /// Exact bytes the client sent — the digest is computed over these.
    pub raw: Vec<u8>,
}

/// Both variants are small (the largest is a few hundred bytes) and the
/// manifest is always held by value during a selection walk.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone)]
pub enum ManifestKind {
    Image(ImageManifest),
    Index(IndexManifest),
}

/// Media types recognised by [`Manifest::parse`].
pub const MT_IMAGE: &str = "application/vnd.oci.image.manifest.v1+json";
pub const MT_INDEX: &str = "application/vnd.oci.image.index.v1+json";
pub const MT_DOCKER_LIST: &str = "application/vnd.docker.distribution.manifest.list.v2+json";
pub const MT_DOCKER_MANIFEST: &str = "application/vnd.docker.distribution.manifest.v2+json";

impl Manifest {
    /// Parse raw JSON bytes into an [`Manifest`].
    ///
    /// Classification follows the spec: an explicit `mediaType` wins; when
    /// it is absent (OCI artifacts frequently omit it) the presence of a
    /// `manifests` array means index, presence of `config` means image.
    pub fn parse(raw: &[u8]) -> Result<Manifest, ManifestParseError> {
        let value: serde_json::Value = serde_json::from_slice(raw).map_err(|e| {
            ManifestParseError(format!("manifest is not valid JSON: {e}"))
        })?;
        let media_type = value
            .get("mediaType")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let is_index = match media_type.as_deref() {
            Some(MT_INDEX) | Some(MT_DOCKER_LIST) => true,
            Some(MT_IMAGE) | Some(MT_DOCKER_MANIFEST) => false,
            Some(other) => {
                return Err(ManifestParseError(format!(
                    "unsupported manifest mediaType `{other}`"
                )))
            }
            None => value.get("manifests").is_some(),
        };

        if is_index {
            let idx: IndexManifest = serde_json::from_value(value).map_err(|e| {
                ManifestParseError(format!("invalid image index: {e}"))
            })?;
            if idx.manifests.is_empty() {
                return Err(ManifestParseError(
                    "image index has an empty `manifests` array".to_string(),
                ));
            }
            Ok(Manifest {
                schema_version: idx.schema_version,
                media_type: idx.media_type.clone(),
                kind: ManifestKind::Index(idx),
                raw: raw.to_vec(),
            })
        } else {
            let img: ImageManifest = serde_json::from_value(value).map_err(|e| {
                ManifestParseError(format!("invalid image manifest: {e}"))
            })?;
            Ok(Manifest {
                schema_version: img.schema_version,
                media_type: img.media_type.clone(),
                kind: ManifestKind::Image(img),
                raw: raw.to_vec(),
            })
        }
    }

    pub fn is_index(&self) -> bool {
        matches!(self.kind, ManifestKind::Index(_))
    }

    /// Child descriptors of an index (empty for an image manifest).
    pub fn children(&self) -> &[Descriptor] {
        match &self.kind {
            ManifestKind::Index(i) => &i.manifests,
            ManifestKind::Image(_) => &[],
        }
    }
}
