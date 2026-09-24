//! Core deterministic packaging logic.
//!
//! # Determinism contract
//!
//! Given a source tree with the same *content* (files, directory layout,
//! symlink targets and the executable bits of regular files), the produced
//! archive bytes are identical no matter:
//!
//! * the order in which the OS enumerates directory entries (`readdir` order),
//! * the host time zone or locale,
//! * file modification/access times, owning uid/gid, or host-specific mode
//!   bits,
//! * where on disk the source tree lives (absolute path is never archived).
//!
//! Rules (this is the normative list used by the implementation):
//!
//! 1. **Fixed ordering.** Entries are sorted lexicographically by archive path
//!    compared as raw bytes. The root is always the first entry (`.`).
//! 2. **Canonical archive paths.** Every component must be valid UTF-8 and
//!    must not be empty, `.` or `..`. There is no `.`/`..`/`//` rewriting:
//!    such names are rejected. Symlink targets are validated lexically; any
//!    target escaping the source root is rejected.
//! 3. **No traversal through symlinked directories.** A symlink whose target
//!    exists and resolves to a directory (including through another symlink,
//!    including a symlink loop, including when the target escapes the root)
//!    is rejected. The packager only ever descends real directories, so two
//!    distinct archive paths can never alias the same file through a symlink.
//! 4. **No hard-link aliases.** Two distinct archive paths whose real
//!    (`stat`, i.e. device+inode) identity is equal are rejected as a
//!    canonical-path conflict.
//! 5. **Permission mapping.** Regular files become `0644`, or `0755` if any
//!    of the owner/group/other execute bits is set. Directories become
//!    `0755`. Symlinks become `0777`. setuid/setgid/sticky bits are stripped.
//! 6. **Fixed timestamps/ownership/format.** Every entry is emitted as a GNU
//!    tar header with uid = gid = 0, uname/gname empty and mtime =
//!    `mtime_epoch` from [`PackOptions`] (default `0`, the Unix epoch).
//! 7. **No special files.** Only regular files, directories and symlinks are
//!    accepted; devices, fifos and sockets are rejected.
//!
//! Alongside `<name>.tar` the packager writes `<name>.manifest.json`
//! (sorted entries, fixed field order) and `<name>.sha256` containing the
//! archive digest (`<hex>  <name>.tar\n`, coreutils-compatible).

use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Archive entry mtime used when the caller does not override it: 1970-01-01T00:00:00Z.
pub const DEFAULT_MTIME_EPOCH: i64 = 0;
/// Mode emitted for non-executable regular files.
pub const MODE_FILE: u32 = 0o644;
/// Mode emitted for executable regular files and directories.
pub const MODE_EXEC: u32 = 0o755;
/// Mode emitted for symbolic links (the conventional mode for a symlink).
pub const MODE_SYMLINK: u32 = 0o777;
/// Manifest schema version produced by this implementation.
pub const MANIFEST_VERSION: u32 = 1;

/// Error type shared by the packager.
#[derive(Debug)]
pub enum Error {
    /// Malformed request / unsupported input the caller can fix
    /// (HTTP 400 in the server).
    ///
    /// `code` is a stable machine-readable identifier.
    Invalid { code: &'static str, message: String },
    /// Determinism-policy violation: conflicting canonical paths, escaping
    /// symlink, symlink traversal, hard-link alias (HTTP 409).
    Conflict(String),
    /// Filesystem/IO failure or tar encoding error (HTTP 500).
    Io(std::io::Error),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Invalid { code, message } => write!(f, "{code}: {message}"),
            Error::Conflict(msg) => write!(f, "conflict: {msg}"),
            Error::Io(err) => write!(f, "io error: {err}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(err) => Some(err),
            _ => None,
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        // Surface symlink loops produced by the OS as policy conflicts.
        if err.raw_os_error() == Some(libc_e_loop()) {
            Error::Conflict(format!("symlink loop while resolving path: {err}"))
        } else {
            Error::Io(err)
        }
    }
}

#[cfg(target_os = "linux")]
fn libc_e_loop() -> i32 {
    // ELOOP on Linux; avoids pulling in the libc crate.
    40
}

#[cfg(all(unix, not(target_os = "linux")))]
fn libc_e_loop() -> i32 {
    // Best-effort: only Linux's ELOOP=40 is mapped; elsewhere rely on
    // ErrorKind::FilesystemLoop.
    -1
}

#[cfg(not(unix))]
fn libc_e_loop() -> i32 {
    -1
}

fn invalid(code: &'static str, message: impl Into<String>) -> Error {
    Error::Invalid {
        code,
        message: message.into(),
    }
}

/// Kind of an archive entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EntryKind {
    File,
    Dir,
    Symlink,
}

/// One entry of the content manifest.
///
/// Field order defines JSON key order (serde struct order) and is part of the
/// deterministic manifest format.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManifestEntry {
    /// Archive path, `/`-separated; root is `.`.
    pub path: String,
    /// Entry type.
    pub kind: EntryKind,
    /// POSIX mode after permission mapping, e.g. `"0644"`.
    pub mode: String,
    /// Fixed entry mtime in seconds since the Unix epoch.
    pub mtime_epoch: i64,
    /// File size in bytes (`0` for dirs/symlinks).
    pub size: u64,
    /// SHA-256 of file content, or of the symlink target bytes; `null` for dirs.
    pub content_sha256: Option<String>,
    /// Symlink target as stored; `null` for files/dirs.
    pub link_target: Option<String>,
}

/// Content manifest, serialized as `<name>.manifest.json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub manifest_version: u32,
    /// Format of the payload archive.
    pub archive_format: String,
    /// Fixed mtime applied to every entry.
    pub mtime_epoch: i64,
    pub entry_count: usize,
    pub entries: Vec<ManifestEntry>,
}

/// One filesystem entry discovered during traversal (internal representation).
#[derive(Debug, Clone)]
pub struct CollectedEntry {
    /// Archive path (`a/b/c`; root is `.`).
    pub rel: String,
    pub kind: EntryKind,
    pub mode: u32,
    pub size: u64,
    pub content_sha256: Option<[u8; 32]>,
    pub link_target: Option<Vec<u8>>,
    /// Absolute real path on the host filesystem.
    pub real_path: PathBuf,
    /// (dev, ino) for regular files — used for hard-link alias detection.
    pub identity: Option<(u64, u64)>,
}

/// Options controlling a packaging run.
#[derive(Debug, Clone)]
pub struct PackOptions {
    /// Directory to package.
    pub source: PathBuf,
    /// Directory the three artifacts are written into.
    pub output_dir: PathBuf,
    /// Artifact base name: `<name>.tar`, `<name>.manifest.json`, `<name>.sha256`.
    pub artifact_name: String,
    /// mtime (seconds since epoch) stamped on every entry; defaults to 0.
    pub mtime_epoch: i64,
    /// Overwrite existing artifacts instead of failing.
    pub overwrite: bool,
}

impl PackOptions {
    pub fn new(
        source: impl Into<PathBuf>,
        output_dir: impl Into<PathBuf>,
        name: impl Into<String>,
    ) -> Self {
        PackOptions {
            source: source.into(),
            output_dir: output_dir.into(),
            artifact_name: name.into(),
            mtime_epoch: DEFAULT_MTIME_EPOCH,
            overwrite: false,
        }
    }
}

/// Result of a successful packaging run.
#[derive(Debug, Clone)]
pub struct PackOutcome {
    pub tar_path: PathBuf,
    pub manifest_path: PathBuf,
    pub digest_path: PathBuf,
    pub sha256: String,
    pub archive_size: u64,
    pub entry_count: usize,
    pub manifest: Manifest,
}

// ---------------------------------------------------------------------------
// Traversal
// ---------------------------------------------------------------------------

/// Canonicalizes the source root and validates it is a real directory (not a
/// symlinked root).
pub fn prepare_source(source: &Path) -> Result<PathBuf, Error> {
    let root = fs::canonicalize(source).map_err(|e| {
        invalid(
            "source_not_found",
            format!("cannot resolve source directory {}: {e}", source.display()),
        )
    })?;
    let meta = fs::symlink_metadata(&root)?;
    if !meta.is_dir() {
        return Err(invalid(
            "source_not_directory",
            format!("{} is not a directory", root.display()),
        ));
    }
    Ok(root)
}

/// Traverses `root` and returns entries in fixed (sorted) order, starting
/// with the root entry `.`.
pub fn collect_entries(root: &Path) -> Result<Vec<CollectedEntry>, Error> {
    let mut out: Vec<CollectedEntry> = Vec::new();
    let _root_meta = fs::symlink_metadata(root)?;
    out.push(CollectedEntry {
        rel: ".".to_string(),
        kind: EntryKind::Dir,
        mode: MODE_EXEC,
        size: 0,
        content_sha256: None,
        link_target: None,
        real_path: root.to_path_buf(),
        identity: None,
    });

    let mut seen_inodes: HashSet<(u64, u64)> = HashSet::new();
    walk(root, Path::new(""), &mut out, &mut seen_inodes)?;
    Ok(out)
}

#[cfg(unix)]
fn file_identity(meta: &fs::Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (meta.dev(), meta.ino())
}

#[cfg(not(unix))]
fn file_identity(_meta: &fs::Metadata) -> (u64, u64) {
    (0, 0)
}

fn walk(
    fs_root: &Path,
    rel_dir: &Path,
    out: &mut Vec<CollectedEntry>,
    seen_inodes: &mut HashSet<(u64, u64)>,
) -> Result<(), Error> {
    let abs_dir = if rel_dir.as_os_str().is_empty() {
        fs_root.to_path_buf()
    } else {
        fs_root.join(rel_dir)
    };

    // Rule 1: never trust readdir order — sort by raw component bytes.
    let mut names: Vec<std::ffi::OsString> = fs::read_dir(&abs_dir)?
        .map(|e| e.map(|e| e.file_name()))
        .collect::<Result<Vec<_>, _>>()?;
    names.sort();

    for name in names {
        // Rule 2: components must be valid UTF-8; reject rather than lossy-convert.
        let comp = match name.to_str() {
            Some(s) => s.to_string(),
            None => {
                return Err(invalid(
                    "non_utf8_name",
                    format!(
                        "non-UTF-8 path component under {}: {:?}",
                        rel_dir.display(),
                        name
                    ),
                ))
            }
        };
        check_component(&comp).map_err(|msg| {
            invalid(
                "invalid_path_component",
                format!("path {}/{}: {msg}", rel_dir.display(), comp),
            )
        })?;

        let rel_path = if rel_dir.as_os_str().is_empty() {
            PathBuf::from(&comp)
        } else {
            rel_dir.join(&comp)
        };
        let abs_path = abs_dir.join(&name);
        let sym = fs::symlink_metadata(&abs_path)?;
        let file_type = sym.file_type();

        if file_type.is_symlink() {
            // Rules 2/3: validate the link before accepting it.
            let target = fs::read_link(&abs_path)?;
            validate_symlink(fs_root, &rel_path, &abs_path, &target)?;
            let target_bytes = target_os_bytes(&target, rel_dir)?;
            let mut hasher = Sha256::new();
            hasher.update(&target_bytes);
            out.push(CollectedEntry {
                rel: archive_path(&rel_path),
                kind: EntryKind::Symlink,
                mode: MODE_SYMLINK,
                size: 0,
                content_sha256: Some(hasher.finalize().into()),
                link_target: Some(target_bytes),
                real_path: abs_path,
                identity: None,
            });
        } else if file_type.is_file() {
            // Rule 4: reject hard-link aliases (same dev+ino, different path).
            let id = file_identity(&sym);
            if !seen_inodes.insert(id) {
                return Err(Error::Conflict(format!(
                    "canonical path conflict: {} has the same file identity (dev,ino) as another entry; hard links are not allowed",
                    rel_path.display()
                )));
            }
            let mode = map_file_mode(&sym);
            let size = sym.len();
            let mut hasher = Sha256::new();
            let mut f = File::open(&abs_path)?;
            let mut buf = [0u8; 64 * 1024];
            loop {
                let n = f.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
            }
            out.push(CollectedEntry {
                rel: archive_path(&rel_path),
                kind: EntryKind::File,
                mode,
                size,
                content_sha256: Some(hasher.finalize().into()),
                link_target: None,
                real_path: abs_path,
                identity: Some(id),
            });
        } else if file_type.is_dir() {
            out.push(CollectedEntry {
                rel: archive_path(&rel_path),
                kind: EntryKind::Dir,
                mode: MODE_EXEC,
                size: 0,
                content_sha256: None,
                link_target: None,
                real_path: abs_path.clone(),
                identity: None,
            });
            walk(fs_root, &rel_path, out, seen_inodes)?;
        } else {
            // Rule 7: no special files.
            return Err(invalid(
                "unsupported_file_type",
                format!(
                    "{} is not a regular file, directory or symlink",
                    rel_path.display()
                ),
            ));
        }
    }
    Ok(())
}

/// Rule 5 permission mapping for a regular file.
fn map_file_mode(meta: &fs::Metadata) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // Any execute bit at all makes the archived file executable.
        if meta.mode() & 0o111 != 0 {
            MODE_EXEC
        } else {
            MODE_FILE
        }
    }
    #[cfg(not(unix))]
    {
        let _ = meta;
        MODE_FILE
    }
}

/// Validates a single archive path component against rule 2.
fn check_component(comp: &str) -> Result<(), &'static str> {
    if comp.is_empty() {
        return Err("empty path component");
    }
    if comp == "." || comp == ".." {
        return Err("'.' and '..' components are not allowed");
    }
    if comp.contains('\0') {
        return Err("NUL byte in path");
    }
    if comp.contains('/') {
        return Err("'/' inside a path component");
    }
    Ok(())
}

fn target_os_bytes(target: &Path, rel_dir: &Path) -> Result<Vec<u8>, Error> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let bytes = target.as_os_str().as_bytes().to_vec();
        if bytes.iter().all(|b| *b != 0) {
            Ok(bytes)
        } else {
            Err(invalid(
                "invalid_link_target",
                format!("symlink under {} contains a NUL byte", rel_dir.display()),
            ))
        }
    }
    #[cfg(not(unix))]
    {
        let _ = rel_dir;
        target
            .to_str()
            .map(|s| s.as_bytes().to_vec())
            .ok_or_else(|| invalid("non_utf8_link_target", "non-UTF-8 symlink target"))
    }
}

/// Rule 2/3 symlink validation.
///
/// * absolute targets are rejected;
/// * lexical resolution against the link's parent must stay inside the root;
/// * if the target exists, its OS-resolved real path must stay inside the
///   root, must not be a directory, and must not form a loop.
fn validate_symlink(
    root: &Path,
    rel_path: &Path,
    abs_path: &Path,
    target: &Path,
) -> Result<(), Error> {
    let target_str_repr = target.display();

    if target.is_absolute() {
        return Err(Error::Conflict(format!(
            "canonical path conflict: symlink {} -> {target_str_repr} is absolute; symlinks must stay inside the package root",
            rel_path.display()
        )));
    }

    // Lexical check (handles even non-existent dangling links).
    let parent = rel_path.parent().unwrap_or_else(|| Path::new(""));
    if lexical_join(parent, target).is_none() {
        return Err(Error::Conflict(format!(
            "canonical path conflict: symlink {} -> {target_str_repr} escapes the package root",
            rel_path.display()
        )));
    }

    // Filesystem check when the target exists: canonicalize with a symlink
    // count bound so loops map to a conflict instead of an IO error.
    if let Some(resolved) = resolve_with_loop_bound(abs_path, 40)? {
        let real_root = fs::canonicalize(root)?;
        if !resolved.starts_with(&real_root) {
            return Err(Error::Conflict(format!(
                "canonical path conflict: symlink {} -> {target_str_repr} resolves outside the package root ({})",
                rel_path.display(),
                resolved.display()
            )));
        }
        // Use symlink_metadata so a link-to-a-link-to-a-dir is detected.
        let meta = fs::symlink_metadata(&resolved).map_err(|e| -> Error { e.into() })?;
        if meta.is_dir() {
            return Err(Error::Conflict(format!(
                "canonical path conflict: symlink {} -> {target_str_repr} points to a directory; directory symlinks would alias canonical paths and are not allowed",
                rel_path.display()
            )));
        }
    }
    Ok(())
}

/// Lexically joins a link-relative directory with a (relative) target and
/// normalizes `.`/`..` components without touching the filesystem.
///
/// Returns `None` if a `..` would climb above the package root — i.e. the
/// target lexically escapes. Inputs are always relative here.
fn lexical_join(base: &Path, target: &Path) -> Option<PathBuf> {
    let mut depth: i64 = 0;
    let mut stack: Vec<&std::ffi::OsStr> = Vec::new();
    for part in base.components().chain(target.components()) {
        match part {
            Component::Normal(c) => {
                stack.push(c);
                depth += 1;
            }
            Component::CurDir => {}
            Component::ParentDir => {
                if depth == 0 {
                    return None;
                }
                stack.pop();
                depth -= 1;
            }
            // base/target are always relative here
            Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    let mut p = PathBuf::new();
    for c in stack {
        p.push(c);
    }
    Some(p)
}

/// Canonicalizes `path` but bounds the number of symlink expansions so a
/// symlink loop returns `Ok(None)`-style conflict signaling instead of a raw
/// OS error. Returns:
///
/// * `Ok(Some(path))` — exists and resolved,
/// * `Ok(None)` — dangling (a component does not exist),
/// * `Err(Conflict)` — symlink loop,
/// * `Err(Io)` — another IO failure.
fn resolve_with_loop_bound(path: &Path, max_links: u32) -> Result<Option<PathBuf>, Error> {
    let mut current = path.to_path_buf();
    let mut links = 0u32;
    loop {
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                if meta.file_type().is_symlink() {
                    links += 1;
                    if links > max_links {
                        return Err(Error::Conflict(format!(
                            "symlink loop detected at {} (more than {max_links} expansions)",
                            path.display()
                        )));
                    }
                    let target = fs::read_link(&current)?;
                    current = match target.is_absolute() {
                        true => target,
                        false => current.parent().unwrap_or(Path::new("/")).join(target),
                    };
                    continue;
                }
                // Not a symlink. Canonicalize the existing prefix to clean
                // up `./..` — path exists, so plain canonicalize is safe but
                // would follow the final component; it is not a link now so
                // either works.
                return Ok(Some(normalize_existing(&current)));
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) if e.raw_os_error() == Some(libc_e_loop()) => {
                return Err(Error::Conflict(format!(
                    "symlink loop detected at {}: {e}",
                    path.display()
                )));
            }
            Err(e) => return Err(Error::Io(e)),
        }
    }
}

/// Normalizes a path known to exist without relying on `canonicalize`
/// following the final component.
fn normalize_existing(path: &Path) -> PathBuf {
    // Path exists and the final component is not a symlink at this point, so
    // canonicalize agrees with the resolved location.
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Converts an internal relative `Path` into a `/`-joined UTF-8 archive path.
fn archive_path(p: &Path) -> String {
    p.components()
        .filter_map(|c| match c {
            Component::Normal(s) => s.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/")
}

// ---------------------------------------------------------------------------
// Archive emission
// ---------------------------------------------------------------------------

/// Validates the artifact base name: a single safe filename component.
pub fn validate_artifact_name(name: &str) -> Result<(), Error> {
    if name.is_empty() || name == "." || name == ".." {
        return Err(invalid(
            "invalid_name",
            "artifact name must be a non-empty filename",
        ));
    }
    if name.contains('/') || name.contains('\0') || name.contains('\\') {
        return Err(invalid(
            "invalid_name",
            "artifact name must not contain path separators or NUL",
        ));
    }
    Ok(())
}

/// Builds the tar archive for `entries`, writing it to `w` and returning the
/// SHA-256 hex digest and byte length.
pub fn write_archive<W: Write>(
    entries: &[CollectedEntry],
    mtime_epoch: i64,
    w: W,
) -> Result<(String, u64), Error> {
    let mut hasher = Sha256::new();
    let mut counting = CountingWriter { inner: w, bytes: 0 };
    {
        let mut hash_w = HashingWriter {
            inner: &mut counting,
            hasher: &mut hasher,
        };
        let mut builder = tar::Builder::new(&mut hash_w);
        // GNU format: fixed magic, deterministic; no PAX/ustar-vendored variance.
        builder.mode(tar::HeaderMode::Deterministic);

        for e in entries {
            let mut header = tar::Header::new_gnu();
            // zeroed header already carries GNU magic + version and mtime 0;
            // set every used field explicitly.
            header.set_path(&e.rel).map_err(|err| {
                invalid(
                    "path_too_long",
                    format!(
                        "archive path {} cannot be encoded in a GNU tar header: {err}",
                        e.rel
                    ),
                )
            })?;
            header.set_mode(e.mode);
            header.set_uid(0);
            header.set_gid(0);
            header.set_mtime(mtime_epoch as u64);
            // uname/gname stay empty (header was zero-initialized).
            match e.kind {
                EntryKind::Dir => {
                    header.set_entry_type(tar::EntryType::Directory);
                    header.set_size(0);
                    header.set_cksum();
                    builder.append(&header, std::io::empty())?;
                }
                EntryKind::File => {
                    header.set_entry_type(tar::EntryType::Regular);
                    header.set_size(e.size);
                    header.set_cksum();
                    let mut f = File::open(&e.real_path)?;
                    // Hash the exact bytes emitted and verify length, so a
                    // file changing mid-pack cannot slip through silently.
                    let mut hasher = Sha256::new();
                    {
                        let mut limited = (&mut f).take(e.size);
                        let mut tee = HashingReader {
                            inner: &mut limited,
                            hasher: &mut hasher,
                        };
                        builder.append(&header, &mut tee)?;
                    }
                    let want = e.content_sha256.ok_or_else(|| {
                        invalid("malformed_entry", format!("file {} has no digest", e.rel))
                    })?;
                    let got: [u8; 32] = hasher.finalize().into();
                    if got != want {
                        return Err(Error::Conflict(format!(
                            "file {} changed content during packaging",
                            e.rel
                        )));
                    }
                    // Ensure the file was exactly `size` bytes (no truncation/growth race).
                    let mut extra = [0u8; 1];
                    if f.read(&mut extra)? != 0 {
                        return Err(Error::Conflict(format!(
                            "file {} changed size during packaging",
                            e.rel
                        )));
                    }
                }
                EntryKind::Symlink => {
                    let target = e.link_target.as_deref().ok_or_else(|| {
                        invalid(
                            "malformed_entry",
                            format!("symlink {} has no target", e.rel),
                        )
                    })?;
                    header.set_entry_type(tar::EntryType::Symlink);
                    header.set_size(0);
                    #[cfg(unix)]
                    let target_os: &std::ffi::OsStr = {
                        use std::os::unix::ffi::OsStrExt;
                        std::ffi::OsStr::from_bytes(target)
                    };
                    #[cfg(not(unix))]
                    let target_os =
                        std::ffi::OsStr::new(std::str::from_utf8(target).map_err(|_| {
                            invalid("non_utf8_link_target", "non-UTF-8 symlink target")
                        })?);
                    header.set_link_name(target_os).map_err(|err| {
                        invalid(
                            "link_target_too_long",
                            format!(
                                "symlink target of {} cannot be encoded in a GNU tar header: {err}",
                                e.rel
                            ),
                        )
                    })?;
                    header.set_cksum();
                    builder.append(&header, std::io::empty())?;
                }
            }
        }
        builder.finish()?;
    }
    let digest = hex::encode(hasher.finalize());
    Ok((digest, counting.bytes))
}

struct CountingWriter<W> {
    inner: W,
    bytes: u64,
}

impl<W: Write> Write for CountingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.bytes += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

struct HashingWriter<'a, W> {
    inner: W,
    hasher: &'a mut Sha256,
}

impl<'a, W: Write> Write for HashingWriter<'a, W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hasher.update(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

/// Reader that SHA-256 hashes everything pulled through it, while ensuring
/// short writes from the underlying source surface as errors (`read_exact`
/// semantics used by tar's `io::copy` path via `Read::by_ref`).
struct HashingReader<'a, R> {
    inner: R,
    hasher: &'a mut Sha256,
}

impl<'a, R: Read> Read for HashingReader<'a, R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.hasher.update(&buf[..n]);
        Ok(n)
    }
}

fn mode_string(mode: u32) -> String {
    format!("{mode:04o}")
}

/// Builds the manifest value from scanned entries.
pub fn build_manifest(entries: &[CollectedEntry], mtime_epoch: i64) -> Manifest {
    let rows = entries
        .iter()
        .map(|e| ManifestEntry {
            path: e.rel.clone(),
            kind: e.kind,
            mode: mode_string(e.mode),
            mtime_epoch,
            size: e.size,
            content_sha256: e.content_sha256.map(hex::encode),
            link_target: e
                .link_target
                .as_ref()
                .map(|b| String::from_utf8_lossy(b).into_owned()),
        })
        .collect();
    Manifest {
        manifest_version: MANIFEST_VERSION,
        archive_format: "gnu-tar".to_string(),
        mtime_epoch,
        entry_count: entries.len(),
        entries: rows,
    }
}

/// Serializes the manifest deterministically: 2-space pretty JSON, trailing newline.
pub fn serialize_manifest(manifest: &Manifest) -> Result<Vec<u8>, Error> {
    let mut json =
        serde_json::to_vec_pretty(manifest).map_err(|e| invalid("bad_manifest", e.to_string()))?;
    json.push(b'\n');
    Ok(json)
}

// ---------------------------------------------------------------------------
// Top-level packaging
// ---------------------------------------------------------------------------

/// Packages `opts.source` into the three artifacts under `opts.output_dir`.
pub fn pack(opts: &PackOptions) -> Result<PackOutcome, Error> {
    validate_artifact_name(&opts.artifact_name)?;
    if opts.mtime_epoch < 0 {
        return Err(invalid(
            "invalid_mtime",
            "mtime_epoch must be >= 0 (GNU ustar numeric header has no sign)",
        ));
    }

    let root = prepare_source(&opts.source)?;

    // The output directory must not live inside the source tree: otherwise
    // packaging would (try to) ingest its own output.
    if opts.output_dir.exists() {
        let out_dir = fs::canonicalize(&opts.output_dir)?;
        if out_dir.starts_with(&root) {
            return Err(invalid(
                "output_inside_source",
                format!(
                    "output directory {} resolves inside the source directory {}",
                    out_dir.display(),
                    root.display()
                ),
            ));
        }
    }
    fs::create_dir_all(&opts.output_dir)?;
    let out_dir = fs::canonicalize(&opts.output_dir)?;
    if out_dir.starts_with(&root) {
        return Err(invalid(
            "output_inside_source",
            format!(
                "output directory {} resolves inside the source directory {}",
                out_dir.display(),
                root.display()
            ),
        ));
    }

    let tar_name = format!("{}.tar", opts.artifact_name);
    let manifest_name = format!("{}.manifest.json", opts.artifact_name);
    let digest_name = format!("{}.sha256", opts.artifact_name);
    let tar_path = out_dir.join(&tar_name);
    let manifest_path = out_dir.join(&manifest_name);
    let digest_path = out_dir.join(&digest_name);

    if !opts.overwrite {
        for p in [&tar_path, &manifest_path, &digest_path] {
            if p.exists() {
                return Err(invalid(
                    "artifact_exists",
                    format!("{} already exists (use overwrite: true)", p.display()),
                ));
            }
        }
    }

    let entries = collect_entries(&root)?;
    let manifest = build_manifest(&entries, opts.mtime_epoch);

    // Write to temp files first, then atomically rename into place. The
    // unique suffix makes concurrent pack runs to the same output directory
    // safe on Unix (rename is atomic).
    let unique = format!(
        "{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    let tmp_tar = out_dir.join(format!(".{tar_name}.tmp-{unique}"));
    let tmp_manifest = out_dir.join(format!(".{manifest_name}.tmp-{unique}"));
    let tmp_digest = out_dir.join(format!(".{digest_name}.tmp-{unique}"));

    let result = (|| -> Result<PackOutcome, Error> {
        let tar_file = File::create(&tmp_tar)?;
        let (sha256, archive_size) = write_archive(&entries, opts.mtime_epoch, tar_file)?;

        let digest_line = format!("{sha256}  {tar_name}\n");
        fs::write(&tmp_digest, digest_line.as_bytes())?;
        fs::write(&tmp_manifest, serialize_manifest(&manifest)?)?;

        for p in [&tmp_tar, &tmp_manifest, &tmp_digest] {
            set_file_mode_644(p);
        }

        // Atomic replacement works whether or not the targets exist.
        fs::rename(&tmp_tar, &tar_path)?;
        fs::rename(&tmp_manifest, &manifest_path)?;
        fs::rename(&tmp_digest, &digest_path)?;

        Ok(PackOutcome {
            tar_path,
            manifest_path,
            digest_path,
            sha256,
            archive_size,
            entry_count: entries.len(),
            manifest,
        })
    })();

    if result.is_err() {
        for p in [&tmp_tar, &tmp_manifest, &tmp_digest] {
            let _ = fs::remove_file(p);
        }
    }
    result
}

#[cfg(unix)]
fn set_file_mode_644(p: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(p, fs::Permissions::from_mode(0o644));
}

#[cfg(not(unix))]
fn set_file_mode_644(_p: &Path) {}
