//! Minimal OCI image-layout model, parsed from local fixtures only.
//!
//! Fixture directory layout:
//! ```text
//! <fixtures>/<name>/
//!   oci-layout                 {"imageLayoutVersion": "1.0.0"}
//!   index.json                OCI image index
//!   blobs/sha256/<digest>     content-addressed blobs
//! ```

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::digest::DigestRef;
use crate::error::{Error, Result};

#[derive(Debug, Deserialize)]
pub struct OciLayout {
    #[serde(rename = "imageLayoutVersion")]
    pub image_layout_version: String,
}

#[derive(Debug, Deserialize)]
pub struct Index {
    #[serde(default)]
    pub manifests: Vec<Descriptor>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Descriptor {
    #[serde(rename = "mediaType")]
    pub media_type: String,
    pub digest: String,
    #[serde(default)]
    pub size: Option<i64>,
    #[serde(default)]
    pub annotations: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
pub struct Manifest {
    #[serde(default, rename = "mediaType")]
    pub media_type: Option<String>,
    pub config: Descriptor,
    #[serde(default)]
    pub layers: Vec<Descriptor>,
}

/// Image config — only the parts we need; everything else is ignored.
#[derive(Debug, Deserialize)]
pub struct ImageConfig {
    #[serde(default)]
    pub os: String,
    #[serde(default)]
    pub architecture: String,
    #[serde(default)]
    pub rootfs: RootFs,
}

#[derive(Debug, Default, Deserialize)]
pub struct RootFs {
    #[serde(rename = "type", default)]
    pub fs_type: String,
    #[serde(default)]
    pub diff_ids: Vec<String>,
}

/// Media types accepted for layers and documents.
pub mod media {
    pub const IMAGE_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
    pub const IMAGE_INDEX: &str = "application/vnd.oci.image.index.v1+json";
    pub const IMAGE_CONFIG: &str = "application/vnd.oci.image.config.v1+json";
    pub const LAYER_TAR: &str = "application/vnd.oci.image.layer.v1.tar";
    pub const LAYER_GZIP: &str = "application/vnd.oci.image.layer.v1.tar+gzip";
}

/// Everything the rebuild engine needs to find bytes on disk.
pub struct Store {
    root: PathBuf,
}

impl Store {
    pub fn new(root: impl AsRef<Path>) -> Self {
        Store {
            root: root.as_ref().to_path_buf(),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Whitelist fixture names: a single path component, conservative charset.
    pub fn validate_name(name: &str) -> Result<()> {
        if name.is_empty()
            || name.len() > 128
            || name == "."
            || name == ".."
            || name.contains('/')
            || name.contains('\\')
            || name.contains('\0')
            || !name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        {
            return Err(Error::BadName(name.to_string()));
        }
        Ok(())
    }

    pub fn image_dir(&self, name: &str) -> PathBuf {
        self.root.join(name)
    }

    pub fn blob_path(&self, name: &str, digest: &DigestRef) -> PathBuf {
        self.image_dir(name)
            .join("blobs")
            .join("sha256")
            .join(&digest.hex)
    }

    /// Load + validate the fixture: layout file, index, manifest, config.
    /// Blob existence for layers is checked here too (descriptors are cheap
    /// trust anchors — a missing blob is an invalid image, not a 404).
    pub fn load_image(&self, name: &str) -> Result<LoadedImage> {
        Self::validate_name(name)?;
        let dir = self.image_dir(name);
        if !dir.is_dir() {
            return Err(Error::NotFound(name.to_string()));
        }

        let layout_bytes = read_file(dir.join("oci-layout"))?;
        let layout: OciLayout = serde_json::from_slice(&layout_bytes)
            .map_err(|e| Error::InvalidImage(format!("oci-layout: {e}")))?;
        if layout.image_layout_version != "1.0.0" {
            return Err(Error::InvalidImage(format!(
                "unsupported imageLayoutVersion {}",
                layout.image_layout_version
            )));
        }

        let index_bytes = read_file(dir.join("index.json"))?;
        let index: Index = serde_json::from_slice(&index_bytes)
            .map_err(|e| Error::InvalidImage(format!("index.json: {e}")))?;

        // Pick the manifest: prefer an explicit annotation, else the first
        // manifest descriptor with the OCI manifest media type.
        let manifest_desc = index
            .manifests
            .iter()
            .find(|d| d.media_type == media::IMAGE_MANIFEST)
            .ok_or_else(|| Error::InvalidImage("no OCI image manifest in index.json".into()))?;
        let manifest_digest = DigestRef::parse(&manifest_desc.digest)?;
        let manifest_bytes = self.read_blob(name, &manifest_digest)?;
        // The index is itself content-addressed on disk; verify the digest.
        crate::digest::expect_digest(&manifest_bytes, &manifest_digest)?;
        let manifest: Manifest = serde_json::from_slice(&manifest_bytes)
            .map_err(|e| Error::InvalidImage(format!("manifest: {e}")))?;

        if manifest.layers.is_empty() {
            return Err(Error::InvalidImage("manifest has no layers".into()));
        }
        for (i, layer) in manifest.layers.iter().enumerate() {
            if !matches!(
                layer.media_type.as_str(),
                media::LAYER_TAR | media::LAYER_GZIP
            ) {
                return Err(Error::InvalidImage(format!(
                    "layer #{i} has unsupported media type {}",
                    layer.media_type
                )));
            }
            let d = DigestRef::parse(&layer.digest)?;
            let p = self.blob_path(name, &d);
            if !p.is_file() {
                return Err(Error::InvalidImage(format!(
                    "layer #{i} blob {} is missing",
                    d.as_str()
                )));
            }
        }

        let config_digest = DigestRef::parse(&manifest.config.digest)?;
        let config_bytes = self.read_blob(name, &config_digest)?;
        crate::digest::expect_digest(&config_bytes, &config_digest)?;
        let config: ImageConfig = serde_json::from_slice(&config_bytes)
            .map_err(|e| Error::InvalidImage(format!("config: {e}")))?;
        if !config.rootfs.diff_ids.is_empty()
            && config.rootfs.diff_ids.len() != manifest.layers.len()
        {
            return Err(Error::InvalidImage(format!(
                "config rootfs.diff_ids count {} != layer count {}",
                config.rootfs.diff_ids.len(),
                manifest.layers.len()
            )));
        }

        Ok(LoadedImage {
            name: name.to_string(),
            manifest,
            manifest_digest,
            config,
            config_digest,
        })
    }

    pub fn read_blob(&self, name: &str, digest: &DigestRef) -> Result<Vec<u8>> {
        let p = self.blob_path(name, digest);
        read_file(p)
    }
}

fn read_file(p: PathBuf) -> Result<Vec<u8>> {
    std::fs::read(&p).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            Error::InvalidImage(format!("missing file {}", p.display()))
        } else {
            Error::io(format!("read {}: {e}", p.display()))
        }
    })
}

/// Loaded + parsed image with digests already verified for config/manifest.
pub struct LoadedImage {
    pub name: String,
    pub manifest: Manifest,
    pub manifest_digest: DigestRef,
    pub config: ImageConfig,
    #[allow(dead_code)]
    pub config_digest: DigestRef,
}

impl LoadedImage {
    pub fn layers(&self) -> &[Descriptor] {
        &self.manifest.layers
    }
}
