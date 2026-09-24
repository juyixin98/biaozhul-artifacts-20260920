//! End-to-end rebuild orchestration.
//!
//! Order of operations for a rebuild:
//!   1. load + parse the fixture (manifest/config already digest-verified)
//!   2. create a fresh staging directory (never a previously published root)
//!   3. for every layer, in order: verify pass then apply pass — the whole
//!      rebuild runs inside the staging tree only
//!   4. compute the final aggregate rootfs digest over a canonical manifest
//!   5. atomically publish: rename staging to a content-addressed build dir and
//!      repoint `latest`; any failure cleans staging and leaves `latest` alone
//!
//! Because no path is ever published before *all* layers are verified and
//! applied, a failed rebuild never exposes a partial root.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use sha2::{Digest as _, Sha256};

use crate::digest::DigestRef;
use crate::error::{Error, Result};
use crate::layer::{self, LayerReport, RootfsState};
use crate::limits::Limits;
use crate::model::{media, LoadedImage, Store};

/// Result returned to the API/CLI.
#[derive(Debug, Serialize)]
pub struct RebuildResult {
    pub image: String,
    pub build_id: String,
    pub rootfs_path: String,
    pub final_digest: String,
    pub config_digest: String,
    pub manifest_digest: String,
    pub layers: Vec<LayerDigestInfo>,
    pub files: Vec<FileInfo>,
    pub status: &'static str,
}

#[derive(Debug, Serialize)]
pub struct LayerDigestInfo {
    pub index: usize,
    pub digest: String,
    pub media_type: String,
    pub compressed_size: u64,
    pub decompressed_size: u64,
    pub payload_bytes: u64,
    pub entries_seen: u64,
    pub files: u64,
    pub dirs: u64,
    pub symlinks: u64,
    pub hardlinks: u64,
    pub whiteouts: u64,
    pub opaque_dirs: u64,
}

#[derive(Debug, Serialize)]
pub struct FileInfo {
    pub path: String,
    pub kind: String,
    pub layer_index: usize,
    pub layer_digest: String,
}

/// Where builds are published.
pub struct Builds {
    dir: PathBuf,
}

impl Builds {
    pub fn new(dir: impl AsRef<Path>) -> Result<Self> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir).map_err(Error::io)?;
        Ok(Builds { dir })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn temp_dir(&self, image: &str) -> Result<PathBuf> {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let pid = std::process::id();
        let p = self.dir.join(format!(".staging-{image}-{pid}-{nanos}"));
        std::fs::create_dir_all(&p).map_err(Error::io)?;
        Ok(p)
    }
}

/// Rebuild one fixture image end to end.
pub fn rebuild(
    store: &Store,
    builds: &Builds,
    image: &str,
    limits: &Limits,
) -> Result<RebuildResult> {
    let loaded = store.load_image(image)?;
    rebuild_loaded(store, builds, loaded, limits)
}

fn rebuild_loaded(
    store: &Store,
    builds: &Builds,
    loaded: LoadedImage,
    limits: &Limits,
) -> Result<RebuildResult> {
    let staging = builds.temp_dir(&loaded.name)?;
    let rootfs = staging.join("rootfs");
    std::fs::create_dir_all(&rootfs).map_err(Error::io)?;

    let outcome = run_layers(store, &loaded, limits, &rootfs);
    if let Err(e) = outcome {
        // Failure: staging is discarded; no published root is ever touched.
        let _ = std::fs::remove_dir_all(&staging);
        return Err(e);
    }
    let (state, reports) = outcome.unwrap();

    let final_digest = compute_final_digest(&loaded, &reports, &state)?;
    let build_id = final_digest
        .strip_prefix("sha256:")
        .unwrap_or(&final_digest)
        .get(..16)
        .unwrap_or("build")
        .to_string();

    let final_dir = builds.dir().join(&loaded.name).join(&build_id);
    if final_dir.exists() {
        // Same content already published; replace with the freshly-verified one.
        std::fs::remove_dir_all(&final_dir).map_err(Error::io)?;
    }
    std::fs::create_dir_all(final_dir.parent().unwrap()).map_err(Error::io)?;
    std::fs::rename(&staging, &final_dir).map_err(Error::io)?;

    // Atomically repoint `latest` via a symlink swap.
    let image_latest = builds.dir().join(&loaded.name).join("latest");
    let tmp_link = builds
        .dir()
        .join(&loaded.name)
        .join(format!(".latest.tmp.{}", std::process::id()));
    let _ = std::fs::remove_file(&tmp_link);
    std::os::unix::fs::symlink(&build_id, &tmp_link).map_err(Error::io)?;
    std::fs::rename(&tmp_link, &image_latest).map_err(Error::io)?;

    let published_rootfs = final_dir.join("rootfs");

    let files = state
        .entries()
        .into_iter()
        .map(|e| FileInfo {
            path: e.path,
            kind: e.kind.to_string(),
            layer_index: e.layer_index,
            layer_digest: e.layer_digest,
        })
        .collect();

    Ok(RebuildResult {
        image: loaded.name.clone(),
        build_id,
        rootfs_path: published_rootfs.display().to_string(),
        final_digest,
        config_digest: loaded.config_digest.as_str(),
        manifest_digest: loaded.manifest_digest.as_str(),
        layers: reports.into_iter().map(layer_info).collect(),
        files,
        status: "published",
    })
}

#[allow(clippy::type_complexity)]
fn run_layers(
    store: &Store,
    loaded: &LoadedImage,
    limits: &Limits,
    rootfs: &Path,
) -> Result<(RootfsState, Vec<LayerReport>)> {
    let mut state = RootfsState::default();
    let mut reports = Vec::with_capacity(loaded.layers().len());

    for (index, desc) in loaded.layers().iter().enumerate() {
        let expected = DigestRef::parse(&desc.digest)?;
        if let Some(declared) = desc.size {
            if declared >= 0 && (declared as u64) > limits.max_compressed_bytes {
                return Err(Error::limit(format!(
                    "layer #{index} declares {declared} compressed bytes, limit {}",
                    limits.max_compressed_bytes
                )));
            }
        }
        let blob = store.blob_path(&loaded.name, &expected);
        let gzip = desc.media_type == media::LAYER_GZIP;
        let expected_diff = loaded.config.rootfs.diff_ids.get(index).map(String::as_str);

        // Pass 1 — verify (compressed blob digest + uncompressed diff_id)
        // before this layer mutates anything.
        layer::scan_verify(
            index,
            &blob,
            &expected.as_str(),
            expected_diff,
            gzip,
            limits,
        )?;
        // Pass 2 — apply.
        let outcome = layer::scan_apply(
            index,
            &blob,
            &expected.as_str(),
            desc.media_type.clone(),
            gzip,
            limits,
            rootfs,
            &mut state,
        )?;
        reports.push(outcome.report);
    }
    Ok((state, reports))
}

fn layer_info(r: LayerReport) -> LayerDigestInfo {
    LayerDigestInfo {
        index: r.index,
        digest: r.digest,
        media_type: r.media_type,
        compressed_size: r.compressed_size,
        decompressed_size: r.decompressed_size,
        payload_bytes: r.payload_bytes,
        entries_seen: r.entries_seen,
        files: r.files,
        dirs: r.dirs,
        symlinks: r.symlinks,
        hardlinks: r.hardlinks,
        whiteouts: r.whiteouts,
        opaque_dirs: r.opaque_dirs,
    }
}

/// Final aggregate digest — a real SHA-256 over a canonical, reproducible
/// serialization of: image identity, config/manifest digests, ordered layer
/// digests with sizes, and the sorted provenance file list. This binds the
/// digest to *both* content provenance and layer history.
fn compute_final_digest(
    loaded: &LoadedImage,
    reports: &[LayerReport],
    state: &RootfsState,
) -> Result<String> {
    let mut h = Sha256::new();
    h.update(b"oci-layered-whitelist-unpack v1\n");
    h.update(format!("image={}\n", loaded.name).as_bytes());
    h.update(format!("config={}\n", loaded.config_digest.as_str()).as_bytes());
    h.update(format!("manifest={}\n", loaded.manifest_digest.as_str()).as_bytes());
    h.update(b"layers:\n");
    for r in reports {
        h.update(
            format!(
                "  {} {} compressed={} decompressed={} payload={}\n",
                r.index, r.digest, r.compressed_size, r.decompressed_size, r.payload_bytes
            )
            .as_bytes(),
        );
    }
    h.update(b"files:\n");
    for entry in state.entries() {
        h.update(
            format!(
                "  {} {} {} {}\n",
                entry.path, entry.kind, entry.layer_index, entry.layer_digest
            )
            .as_bytes(),
        );
    }
    Ok(format!("sha256:{}", hex::encode(h.finalize())))
}
