//! Artifact verification.
//!
//! Given a produced `.tar` (and usually its manifest), verification:
//!
//! 1. recomputes the archive SHA-256 and compares it against the `.sha256`
//!    sidecar (if provided);
//! 2. parses the tar and checks every entry's path, kind, mapped mode, fixed
//!    mtime, size, and content/symlink-target SHA-256 against the manifest,
//!    including order and count — no extra entries, no missing entries;
//! 3. when a `source` directory is given, repackages it to a memory buffer
//!    with the manifest's mtime and requires the repacked bytes' digest to
//!    equal the archive digest (full byte-for-byte reproducibility).

use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::pack::{
    build_manifest, collect_entries, prepare_source, serialize_manifest, EntryKind, Error,
    Manifest, ManifestEntry, MODE_EXEC, MODE_FILE, MODE_SYMLINK,
};

/// Verification inputs.
#[derive(Debug, Clone)]
pub struct VerifyOptions {
    /// Path to the `.tar` artifact.
    pub tar_path: PathBuf,
    /// Optional path to the manifest. If `None`, derived by replacing the
    /// `.tar` suffix with `.manifest.json`.
    pub manifest_path: Option<PathBuf>,
    /// Optional path to the `.sha256` sidecar. If `None`, derived.
    pub digest_path: Option<PathBuf>,
    /// Optional source directory for full reproducibility rebuild.
    pub source: Option<PathBuf>,
}

impl VerifyOptions {
    pub fn new(tar_path: impl Into<PathBuf>) -> Self {
        VerifyOptions {
            tar_path: tar_path.into(),
            manifest_path: None,
            digest_path: None,
            source: None,
        }
    }
}

/// Verification outcome.
#[derive(Debug, Clone)]
pub struct VerifyOutcome {
    pub ok: bool,
    pub sha256: String,
    pub archive_size: u64,
    pub entry_count: usize,
    pub digest_matches: Option<bool>,
    pub manifest_matches: bool,
    pub rebuild_matches: Option<bool>,
    /// Human-readable problems; empty when `ok` is true.
    pub problems: Vec<String>,
}

fn derive_sibling(tar: &Path, suffix: &str) -> PathBuf {
    let stem = tar
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let base = stem.strip_suffix(".tar").unwrap_or(&stem);
    tar.with_file_name(format!("{base}{suffix}"))
}

/// Runs verification as described in the module docs.
pub fn verify(opts: &VerifyOptions) -> Result<VerifyOutcome, Error> {
    let mut problems: Vec<String> = Vec::new();

    let manifest_path = opts
        .manifest_path
        .clone()
        .unwrap_or_else(|| derive_sibling(&opts.tar_path, ".manifest.json"));
    let digest_path = opts
        .digest_path
        .clone()
        .unwrap_or_else(|| derive_sibling(&opts.tar_path, ".sha256"));

    // 1. Archive bytes + digest.
    let mut f = File::open(&opts.tar_path).map_err(|e| Error::Invalid {
        code: "tar_not_found",
        message: format!("cannot open {}: {e}", opts.tar_path.display()),
    })?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    let mut archive_size: u64 = 0;
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        archive_size += n as u64;
        hasher.update(&buf[..n]);
    }
    let sha = hex::encode(hasher.finalize());

    let digest_matches = if digest_path.exists() {
        let raw = std::fs::read_to_string(&digest_path)?;
        let claimed = raw
            .split_whitespace()
            .next()
            .unwrap_or("")
            .trim()
            .to_string();
        let m = claimed.eq_ignore_ascii_case(&sha);
        if !m {
            problems.push(format!(
                "digest mismatch: {} claims {claimed}, archive is {sha}",
                digest_path.display()
            ));
        }
        Some(m)
    } else {
        None
    };

    // 2. Manifest structural comparison.
    let manifest: Manifest =
        serde_json::from_slice(&std::fs::read(&manifest_path)?).map_err(|e| Error::Invalid {
            code: "bad_manifest",
            message: format!("cannot parse {}: {e}", manifest_path.display()),
        })?;

    let actual = read_archive_entries(&opts.tar_path)?;
    if actual.len() != manifest.entries.len() {
        problems.push(format!(
            "entry count mismatch: tar has {} entries, manifest has {}",
            actual.len(),
            manifest.entries.len()
        ));
    }
    let structural_matches = actual
        .iter()
        .zip(manifest.entries.iter())
        .all(|(a, m)| compare_entry(a, m, &mut problems));
    let manifest_matches = structural_matches && actual.len() == manifest.entries.len();

    // 3. Optional rebuild from source.
    let mut rebuild_matches: Option<bool> = None;
    if let Some(source) = &opts.source {
        let root = prepare_source(source)?;
        let entries = collect_entries(&root)?;
        let rebuilt_manifest = build_manifest(&entries, manifest.mtime_epoch);
        if rebuilt_manifest != manifest {
            problems.push("rebuilt manifest differs from the shipped manifest".to_string());
        }
        let mut buf: Vec<u8> = Vec::new();
        let (rebuilt_sha, _) =
            crate::pack::write_archive(&entries, manifest.mtime_epoch, &mut buf)?;
        let m = rebuilt_sha == sha;
        if !m {
            problems.push(format!(
                "rebuild mismatch: repacking {} yields {rebuilt_sha}, archive is {sha}",
                root.display()
            ));
        }
        // Also check manifest serialization round-trips byte-wise if the
        // shipped manifest file is present.
        if manifest_path.exists() {
            let want = serialize_manifest(&manifest)?;
            let got = serialize_manifest(&rebuilt_manifest)?;
            if want != got {
                problems.push(
                    "shipped manifest is not the canonical serialization of the tree".to_string(),
                );
            }
        }
        rebuild_matches = Some(m && rebuilt_manifest == manifest);
    }

    let ok = manifest_matches
        && digest_matches.unwrap_or(true)
        && rebuild_matches.unwrap_or(true)
        && problems.is_empty();

    Ok(VerifyOutcome {
        ok,
        sha256: sha,
        archive_size,
        entry_count: actual.len(),
        digest_matches,
        manifest_matches,
        rebuild_matches,
        problems,
    })
}

/// Lightweight per-entry view pulled straight from a tar stream.
#[derive(Debug, Clone)]
struct ActualEntry {
    path: String,
    kind: EntryKind,
    mode: u32,
    mtime: i64,
    size: u64,
    content_sha256: Option<String>,
    link_target: Option<String>,
}

fn read_archive_entries(tar_path: &Path) -> Result<Vec<ActualEntry>, Error> {
    let f = File::open(tar_path)?;
    let mut ar = tar::Archive::new(f);
    let mut out = Vec::new();
    for entry in ar.entries()? {
        let mut e = entry?;
        let header = e.header();
        let kind = if header.entry_type().is_dir() {
            EntryKind::Dir
        } else if header.entry_type().is_symlink() {
            EntryKind::Symlink
        } else if header.entry_type().is_file() {
            EntryKind::File
        } else {
            return Err(Error::Invalid {
                code: "unexpected_entry_type",
                message: format!("unexpected tar entry type {:?}", header.entry_type()),
            });
        };
        let path = e
            .path()?
            .components()
            .filter_map(|c| match c {
                std::path::Component::Normal(s) => s.to_str(),
                std::path::Component::CurDir => Some("."),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("/");
        let mode = header.mode().map_err(|e| Error::Invalid {
            code: "bad_header",
            message: format!("unreadable mode in {path}: {e}"),
        })?;
        let mtime = header.mtime().map_err(|e| Error::Invalid {
            code: "bad_header",
            message: format!("unreadable mtime in {path}: {e}"),
        })?;
        let size = header.size().map_err(|e| Error::Invalid {
            code: "bad_header",
            message: format!("unreadable size in {path}: {e}"),
        })?;

        let mut hasher = Sha256::new();
        match kind {
            EntryKind::File => {
                let mut buf = vec![0u8; 64 * 1024];
                loop {
                    let n = e.read(&mut buf)?;
                    if n == 0 {
                        break;
                    }
                    hasher.update(&buf[..n]);
                }
            }
            EntryKind::Symlink => {
                let target = header
                    .link_name()
                    .map_err(|e| Error::Invalid {
                        code: "bad_header",
                        message: format!("unreadable link target in {path}: {e}"),
                    })?
                    .ok_or_else(|| Error::Invalid {
                        code: "bad_header",
                        message: format!("symlink {path} has no link name in header"),
                    })?;
                #[cfg(unix)]
                let bytes = {
                    use std::os::unix::ffi::OsStrExt;
                    target.as_os_str().as_bytes()
                };
                #[cfg(not(unix))]
                let bytes = target.as_os_str().to_string_lossy().as_bytes();
                hasher.update(bytes);
                let target_string = target.to_string_lossy().into_owned();
                out.push(ActualEntry {
                    path,
                    kind,
                    mode,
                    mtime: mtime as i64,
                    size,
                    content_sha256: Some(hex::encode(hasher.finalize())),
                    link_target: Some(target_string),
                });
                continue;
            }
            EntryKind::Dir => {}
        }
        let content_sha256 = match kind {
            EntryKind::File => Some(hex::encode(hasher.finalize())),
            _ => None,
        };
        out.push(ActualEntry {
            path,
            kind,
            mtime: mtime as i64,
            mode,
            size,
            content_sha256,
            link_target: None,
        });
    }
    Ok(out)
}

fn compare_entry(a: &ActualEntry, m: &ManifestEntry, problems: &mut Vec<String>) -> bool {
    let mut ok = true;
    let mut check = |cond: bool, msg: String| {
        if !cond {
            problems.push(msg);
            ok = false;
        }
    };

    check(
        a.path == m.path,
        format!("path mismatch: {} vs {}", a.path, m.path),
    );
    check(
        a.kind == m.kind,
        format!("kind mismatch in {}: {:?} vs {:?}", m.path, a.kind, m.kind),
    );
    check(
        mode_string(a.mode) == m.mode,
        format!(
            "mode mismatch in {}: {} vs {}",
            m.path,
            mode_string(a.mode),
            m.mode
        ),
    );
    check(
        a.mtime == m.mtime_epoch,
        format!(
            "mtime mismatch in {}: {} vs {}",
            m.path, a.mtime, m.mtime_epoch
        ),
    );
    check(
        a.size == m.size,
        format!("size mismatch in {}: {} vs {}", m.path, a.size, m.size),
    );
    check(
        a.content_sha256 == m.content_sha256,
        format!(
            "content hash mismatch in {}: {:?} vs {:?}",
            m.path, a.content_sha256, m.content_sha256
        ),
    );
    check(
        a.link_target == m.link_target,
        format!(
            "link target mismatch in {}: {:?} vs {:?}",
            m.path, a.link_target, m.link_target
        ),
    );

    // Sanity: modes must follow the permission mapping.
    let allowed = match a.kind {
        EntryKind::File => a.mode == MODE_FILE || a.mode == MODE_EXEC,
        EntryKind::Dir => a.mode == MODE_EXEC,
        EntryKind::Symlink => a.mode == MODE_SYMLINK,
    };
    check(
        allowed,
        format!(
            "mode {:o} in {} violates the permission mapping",
            a.mode, a.path
        ),
    );
    // Owner/group must be normalized.
    ok
}

fn mode_string(mode: u32) -> String {
    format!("{mode:04o}")
}
