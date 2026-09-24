//! Minimal, read-only OCI image layout support for local fixtures.
//!
//! Supported layout (OCI image spec v1):
//!
//! ```text
//! <image>/
//!   oci-layout          -> {"imageLayoutVersion":"1.0.0"}
//!   index.json          -> single manifest descriptor (manifest.json)
//!   blobs/sha256/<hex>  -> content-addressed blobs
//! ```
//!
//! Legacy Docker manifest media types (`application/vnd.docker.distribution.*`)
//! are accepted as well, because local `docker save`-style fixtures use them.

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::digest::OciDigest;
use crate::error::{AppError, AppResult};

// ---- index.json ----

#[derive(Debug, Deserialize)]
pub struct ImageIndex {
    #[serde(rename = "schemaVersion")]
    pub schema_version: u32,
    pub manifests: Vec<Descriptor>,
}

#[derive(Debug, Deserialize)]
pub struct Descriptor {
    #[serde(rename = "mediaType")]
    pub media_type: Option<String>,
    pub digest: OciDigest,
    pub size: Option<u64>,
}

// ---- manifest ----

#[derive(Debug, Deserialize)]
pub struct Manifest {
    #[serde(rename = "schemaVersion")]
    pub schema_version: u32,
    pub config: Descriptor,
    pub layers: Vec<Descriptor>,
}

// ---- image config (only `history` is optional context; fields unused) ----

#[derive(Debug, Deserialize)]
pub struct ImageConfig {
    #[serde(default)]
    pub architecture: String,
    #[serde(default)]
    pub os: String,
}

const SUPPORTED_MANIFEST_MEDIA: &[&str] = &[
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
];

const SUPPORTED_LAYER_MEDIA: &[&str] = &[
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
];

/// How a layer blob is compressed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LayerCompression {
    Plain,
    Gzip,
}

/// One layer to apply, in order.
#[derive(Debug, Clone)]
pub struct LayerRef {
    pub digest: OciDigest,
    pub blob: PathBuf,
    pub compression: LayerCompression,
}

/// Everything the rebuild needs from the on-disk OCI layout.
pub struct ImageLayout {
    pub root: PathBuf,
    pub manifest_digest: OciDigest,
    pub layers: Vec<LayerRef>,
    /// Raw config blob bytes (digest already verified by the caller flow).
    #[allow(dead_code)]
    pub config: Vec<u8>,
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> AppResult<T> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            AppError::layout(format!("missing file: {}", path.display()))
        } else {
            AppError::Io(e)
        }
    })?;
    serde_json::from_slice(&bytes)
        .map_err(|e| AppError::layout(format!("invalid JSON in {}: {e}", path.display())))
}

impl ImageLayout {
    /// Open an OCI image layout directory and resolve manifest + config.
    /// Digests of the manifest and config are verified here; layer digests
    /// are verified separately (streaming) by the rebuild pipeline.
    pub fn open(root: &Path) -> AppResult<Self> {
        if !root.is_dir() {
            return Err(AppError::NotFound(format!(
                "image directory not found: {}",
                root.display()
            )));
        }

        // oci-layout marker
        let marker = root.join("oci-layout");
        let marker_bytes = std::fs::read(&marker)
            .map_err(|_| AppError::layout("missing oci-layout marker file".to_string()))?;
        let marker_val: serde_json::Value = serde_json::from_slice(&marker_bytes)
            .map_err(|_| AppError::layout("oci-layout marker is not valid JSON".to_string()))?;
        if marker_val
            .get("imageLayoutVersion")
            .and_then(|v| v.as_str())
            != Some("1.0.0")
        {
            return Err(AppError::layout(
                "unsupported imageLayoutVersion (only 1.0.0)".to_string(),
            ));
        }

        // index.json
        let index: ImageIndex = read_json(&root.join("index.json"))?;
        if index.schema_version != 2 {
            return Err(AppError::layout(format!(
                "unexpected index schemaVersion {}",
                index.schema_version
            )));
        }
        let manifest_desc = index.manifests.into_iter().next().ok_or_else(|| {
            AppError::layout("index.json contains no manifest descriptor".to_string())
        })?;
        if let Some(mt) = &manifest_desc.media_type {
            if !SUPPORTED_MANIFEST_MEDIA.contains(&mt.as_str()) {
                return Err(AppError::Manifest(format!(
                    "unsupported manifest media type: {mt}"
                )));
            }
        }

        // manifest blob (digest verified over exact bytes)
        let manifest_bytes = Self::read_verified_blob(root, &manifest_desc.digest, "manifest")?;
        let manifest: Manifest = serde_json::from_slice(&manifest_bytes)
            .map_err(|e| AppError::Manifest(format!("invalid manifest: {e}")))?;
        if manifest.schema_version != 2 {
            return Err(AppError::Manifest(format!(
                "unexpected manifest schemaVersion {}",
                manifest.schema_version
            )));
        }

        // config blob (digest verified)
        let config_bytes = Self::read_verified_blob(root, &manifest.config.digest, "config")?;
        let config: ImageConfig = serde_json::from_slice(&config_bytes)
            .map_err(|e| AppError::Manifest(format!("invalid image config: {e}")))?;
        tracing::debug!(architecture = %config.architecture, os = %config.os, "image config");

        // resolve layers
        let mut layers = Vec::with_capacity(manifest.layers.len());
        for (i, desc) in manifest.layers.iter().enumerate() {
            let media = desc
                .media_type
                .as_deref()
                .ok_or_else(|| AppError::Manifest(format!("layer {i} has no mediaType")))?;
            let compression = match media {
                m if m.ends_with("tar+gzip") || m.ends_with("diff.tar.gzip") => {
                    LayerCompression::Gzip
                }
                m if m.ends_with(".tar") || m.ends_with("diff.tar") => LayerCompression::Plain,
                other => {
                    return Err(AppError::Manifest(format!(
                        "layer {i}: unsupported layer media type: {other}"
                    )));
                }
            };
            if !SUPPORTED_LAYER_MEDIA.contains(&media) {
                return Err(AppError::Manifest(format!(
                    "layer {i}: unsupported layer media type: {media}"
                )));
            }
            let blob = Self::blob_path(root, &desc.digest)?;
            layers.push(LayerRef {
                digest: desc.digest.clone(),
                blob,
                compression,
            });
        }

        Ok(Self {
            root: root.to_path_buf(),
            manifest_digest: manifest_desc.digest,
            layers,
            config: config_bytes,
        })
    }

    /// Resolve `blobs/sha256/<hex>` and reject anything that escapes it
    /// (symlinks, `..`, unexpected algorithm dirs).
    fn blob_path(root: &Path, digest: &OciDigest) -> AppResult<PathBuf> {
        let p = root.join("blobs").join("sha256").join(digest.hex());
        let meta = std::fs::symlink_metadata(&p)
            .map_err(|_| AppError::layout(format!("missing blob for digest {digest}")))?;
        if !meta.is_file() {
            return Err(AppError::layout(format!(
                "blob {digest} is not a regular file"
            )));
        }
        Ok(p)
    }

    /// Read a whole descriptor blob and verify its digest exactly.
    fn read_verified_blob(root: &Path, digest: &OciDigest, what: &str) -> AppResult<Vec<u8>> {
        let path = Self::blob_path(root, digest)?;
        let bytes = std::fs::read(&path)?;
        let actual = OciDigest::of_bytes(&bytes);
        if !digest.verify(&actual) {
            return Err(AppError::DigestMismatch {
                descriptor: what.to_string(),
                expected: digest.to_string(),
                actual: actual.to_string(),
            });
        }
        Ok(bytes)
    }
}
