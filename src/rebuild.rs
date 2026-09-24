//! Rebuild pipeline: verify every layer → extract into a private staging
//! directory → compute the deterministic rootfs digest → atomically publish.
//!
//! Nothing under `workdir/roots/<image>` is replaced until every layer has
//! been verified *and* applied successfully; a failing build leaves the
//! previously published rootfs (if any) untouched and no partial result is
//! published.

use std::fs;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::error::{AppError, AppResult};
use crate::extractor::{Extractor, NodeInfo, NodeKind};
use crate::limits::Limits;
use crate::oci::ImageLayout;

/// One entry in the provenance report.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileRecord {
    /// Path relative to the rootfs, `/`-separated, leading `./` stripped.
    pub path: String,
    pub kind: String,
    /// Digest of the layer that last materialised this path.
    #[serde(rename = "sourceLayer")]
    pub source_layer: String,
    /// Zero-based position of that layer in the image.
    #[serde(rename = "layerIndex")]
    pub layer_index: usize,
    /// SHA-256 of the file's content (`null` for directories/symlinks).
    #[serde(rename = "contentSha256", skip_serializing_if = "Option::is_none")]
    pub content_sha256: Option<String>,
    /// Symlink target, when `kind == "symlink"`.
    #[serde(rename = "symlinkTarget", skip_serializing_if = "Option::is_none")]
    pub symlink_target: Option<String>,
    /// Unix permission bits (octal rendered as string, e.g. "0755").
    pub mode: String,
}

/// Result of a successful rebuild.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RebuildReport {
    pub image: String,
    /// Digest of the image manifest blob (identity of the input).
    #[serde(rename = "manifestDigest")]
    pub manifest_digest: String,
    /// Number of layers applied.
    #[serde(rename = "layerCount")]
    pub layer_count: usize,
    /// Ordered layer digests, in application order.
    pub layers: Vec<String>,
    /// Deterministic digest of the final rootfs tree.
    #[serde(rename = "rootfsDigest")]
    pub rootfs_digest: String,
    pub files: Vec<FileRecord>,
    /// Total number of reported nodes.
    #[serde(rename = "nodeCount")]
    pub node_count: usize,
}

/// Where things live on disk.
pub struct Workdir {
    /// Base directory containing `images/`, `staging/`, `roots/`.
    base: PathBuf,
}

impl Workdir {
    pub fn new(base: &Path) -> Self {
        Self {
            base: base.to_path_buf(),
        }
    }

    pub fn images_dir(&self) -> PathBuf {
        self.base.join("images")
    }
    pub fn staging_dir(&self) -> PathBuf {
        self.base.join("staging")
    }
    pub fn roots_dir(&self) -> PathBuf {
        self.base.join("roots")
    }

    /// Look up an OCI fixture directory by image name. Only a strict
    /// `[a-zA-Z0-9._-]{1,64}` name is accepted (no path separators).
    pub fn image_path(&self, name: &str) -> AppResult<PathBuf> {
        if name.is_empty()
            || name.len() > 64
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        {
            return Err(AppError::BadImageName(name.to_string()));
        }
        Ok(self.images_dir().join(name))
    }

    /// Run a full rebuild. On success returns the report; on failure the
    /// staging directory is cleaned up and no rootfs is published.
    pub fn rebuild(&self, image_name: &str, limits: &Limits) -> AppResult<RebuildReport> {
        let image_dir = self.image_path(image_name)?;
        let layout = ImageLayout::open(&image_dir)?;

        // Fresh, uniquely-named staging root.
        fs::create_dir_all(self.staging_dir())?;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let pid = std::process::id();
        let stage = self
            .staging_dir()
            .join(format!("{image_name}-{pid}-{stamp}"));
        fs::create_dir(&stage)
            .map_err(|e| AppError::Other(anyhow::anyhow!("cannot create staging dir: {e}")))?;

        // Run the risky part; always clean up staging afterwards.
        let outcome = self.rebuild_into(&stage, &layout, limits);
        match outcome {
            Ok(report) => {
                // Publish atomically: rename staging into a digest-named
                // directory, then flip the `latest` pointer via rename.
                self.publish(image_name, &stage, &report.rootfs_digest)?;
                Ok(report)
            }
            Err(e) => {
                let _ = fs::remove_dir_all(&stage);
                tracing::warn!(%image_name, error = %e, "rebuild failed; no partial rootfs published");
                Err(e)
            }
        }
    }

    fn rebuild_into(
        &self,
        stage: &Path,
        layout: &ImageLayout,
        limits: &Limits,
    ) -> AppResult<RebuildReport> {
        // Phase 1 — verify ALL layer blobs before applying anything.
        for layer in &layout.layers {
            Extractor::new(stage.to_path_buf(), limits).verify_layer(layer)?;
        }

        // Phase 2 — apply in order.
        let mut extractor = Extractor::new(stage.to_path_buf(), limits);
        for (i, layer) in layout.layers.iter().enumerate() {
            extractor.apply_layer(layer, i)?;
            extractor.check_file_quota()?;
        }

        // Phase 3 — walk the final tree, build provenance + digest.
        let rootfs_digest = compute_rootfs_digest(stage, &extractor.provenance)?;
        let files = build_file_records(stage, &extractor.provenance)?;

        Ok(RebuildReport {
            image: layout
                .root
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default(),
            manifest_digest: layout.manifest_digest.to_string(),
            layer_count: layout.layers.len(),
            layers: layout.layers.iter().map(|l| l.digest.to_string()).collect(),
            rootfs_digest,
            files,
            node_count: extractor.provenance.len(),
        })
    }

    fn publish(&self, image_name: &str, stage: &Path, digest: &str) -> AppResult<()> {
        let roots = self.roots_dir().join(image_name);
        fs::create_dir_all(&roots)?;
        let final_dir = roots.join(digest);
        if final_dir.exists() {
            // Identical rebuild — discard the new staging copy and reuse.
            fs::remove_dir_all(stage)?;
            return Ok(());
        }
        fs::rename(stage, &final_dir)?;

        // Flip `latest` atomically: write a temp file then rename.
        let tmp = roots.join(format!(".latest.tmp.{}", std::process::id()));
        fs::write(&tmp, digest)?;
        fs::rename(&tmp, roots.join("latest"))?;
        Ok(())
    }

    /// Path to the published rootfs for an image, if a rebuild exists.
    pub fn published_root(&self, image_name: &str) -> AppResult<Option<PathBuf>> {
        let roots = self.roots_dir().join(image_name);
        let pointer = roots.join("latest");
        match fs::read_to_string(&pointer) {
            Ok(digest) => Ok(Some(roots.join(digest.trim()))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }
}

// --------------------------------------------------------------------------
// Deterministic rootfs digest
// --------------------------------------------------------------------------

/// Canonical node record fed into the rootfs Merkle hash.
///
/// Directories hash their children; files hash content; symlinks hash their
/// target. Traversal order is sorted by raw byte name, so the digest is
/// independent of filesystem iteration order and layer application order
/// (it describes the *final* tree only).
fn node_digest(dir: &Path, rel: &Path) -> AppResult<(String, Vec<u8>)> {
    let abs = dir.join(rel);
    let meta = fs::symlink_metadata(&abs)?;
    let mut h = Sha256::new();

    let kind: &[u8];
    if meta.is_dir() {
        kind = b"dir";
        let mut names: Vec<Vec<u8>> = Vec::new();
        for entry in fs::read_dir(&abs)? {
            let entry = entry?;
            names.push(entry.file_name().as_bytes().to_vec());
        }
        names.sort();
        for name in &names {
            let child_rel = if rel.as_os_str().is_empty() {
                PathBuf::from(std::ffi::OsStr::from_bytes(name))
            } else {
                rel.join(std::ffi::OsStr::from_bytes(name))
            };
            let (child_hash, _) = node_digest(dir, &child_rel)?;
            h.update(name);
            h.update([0u8]);
            h.update(child_hash.as_bytes());
        }
    } else if meta.file_type().is_symlink() {
        kind = b"symlink";
        let target = fs::read_link(&abs)?;
        h.update(target.as_os_str().as_bytes());
    } else if meta.is_file() {
        kind = b"file";
        let mut f = fs::File::open(&abs)?;
        let mut buf = [0u8; 64 * 1024];
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            h.update(&buf[..n]);
        }
    } else {
        // Nothing other than dir/file/symlink should ever be extractable,
        // but refuse to digest anything exotic.
        return Err(AppError::rejected(
            rel.display().to_string(),
            "non whitelisted node type reached rootfs",
        ));
    }

    // Mix in mode bits (permission part only, no setuid/setgid/sticky) and
    // node kind to make metadata changes observable in the digest.
    use std::os::unix::fs::MetadataExt;
    let mode = meta.mode() & 0o7777;
    h.update(kind);
    h.update([0u8]);
    h.update(mode.to_le_bytes());

    Ok((hex::encode(h.finalize()), Vec::new()))
}

/// Hash the root of the tree as `sha256(canonical(root-record))`.
pub fn compute_rootfs_digest(
    root: &Path,
    _provenance: &std::collections::HashMap<PathBuf, NodeInfo>,
) -> AppResult<String> {
    let (h, _) = node_digest(root, Path::new(""))?;
    Ok(format!("sha256:{h}"))
}

// --------------------------------------------------------------------------
// Provenance report
// --------------------------------------------------------------------------

fn build_file_records(
    root: &Path,
    provenance: &std::collections::HashMap<PathBuf, NodeInfo>,
) -> AppResult<Vec<FileRecord>> {
    // Sort by path for stable output.
    let mut paths: Vec<&PathBuf> = provenance.keys().collect();
    paths.sort();

    let mut out = Vec::with_capacity(paths.len());
    for rel in paths {
        let info = &provenance[rel];
        let abs = root.join(rel);
        let meta = fs::symlink_metadata(&abs).map_err(|e| {
            AppError::Other(anyhow::anyhow!(
                "provenance references missing path {}: {e}",
                rel.display()
            ))
        })?;

        let (kind, content_sha256, symlink_target) = match info.kind {
            NodeKind::Directory => ("directory".to_string(), None, None),
            NodeKind::Symlink => (
                "symlink".to_string(),
                None,
                Some(fs::read_link(&abs)?.to_string_lossy().into_owned()),
            ),
            NodeKind::File => {
                let mut h = Sha256::new();
                let mut f = fs::File::open(&abs)?;
                let mut buf = [0u8; 64 * 1024];
                loop {
                    let n = f.read(&mut buf)?;
                    if n == 0 {
                        break;
                    }
                    h.update(&buf[..n]);
                }
                (
                    "file".to_string(),
                    Some(format!("sha256:{}", hex::encode(h.finalize()))),
                    None,
                )
            }
        };

        use std::os::unix::fs::MetadataExt;
        let mode = meta.mode() & 0o7777;

        out.push(FileRecord {
            path: rel.to_string_lossy().replace('\\', "/"),
            kind,
            source_layer: info.layer.to_string(),
            layer_index: info.layer_index,
            content_sha256,
            symlink_target,
            mode: format!("{mode:04o}"),
        });
    }
    Ok(out)
}
