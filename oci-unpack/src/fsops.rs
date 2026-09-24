//! Low-level, deliberately small filesystem primitives.
//!
//! Every operation takes an absolute path that has already been proven safe by
//! [`crate::path`]. Operations overwrite/replace by type correctly: replacing a
//! directory with a file (or vice versa) removes the old object first, and
//! symlinks are always handled by *link metadata* (lstat), never followed.

use std::fs;
use std::io::{self, Read};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use crate::error::{Error, Result};

/// Remove whatever `p` is — file, symlink (target never touched), or empty
/// directory. Callers (the layer planner) guarantee that children are removed
/// first when a directory subtree must go.
pub fn remove_any(p: &Path) -> Result<()> {
    let meta = match fs::symlink_metadata(p) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(Error::io(e)),
    };
    let r = if meta.file_type().is_dir() {
        fs::remove_dir(p)
    } else {
        fs::remove_file(p)
    };
    r.or_else(|e| {
        if e.kind() == io::ErrorKind::NotFound {
            Ok(())
        } else {
            Err(Error::io(e))
        }
    })
}

/// Create a directory (no mode set here; callers apply masked modes). Parents
/// are created on demand by the caller with provenance tracking.
pub fn mkdir(p: &Path, mode: u32) -> Result<()> {
    match fs::create_dir(p) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            let meta = fs::symlink_metadata(p).map_err(Error::io)?;
            if !meta.is_dir() {
                return Err(Error::Conflict(p.display().to_string()));
            }
        }
        Err(e) => return Err(Error::io(e)),
    }
    chmod(p, mode)
}

pub fn chmod(p: &Path, mode: u32) -> Result<()> {
    // setuid/setgid/stripped defensively: fixtures never need privilege bits.
    let masked = mode & 0o7777 ^ (mode & 0o6000);
    fs::set_permissions(p, fs::Permissions::from_mode(masked)).map_err(Error::io)
}

/// Atomically replace `dst` (if any) with a regular file containing the data
/// streamed from `reader`. Writes go to a temp file in the same directory so
/// the rename is atomic on the same filesystem.
pub fn write_file_atomic<R: Read>(dst: &Path, mode: u32, mut reader: R) -> Result<u64> {
    let parent = dst
        .parent()
        .ok_or_else(|| Error::Conflict(format!("no parent directory for {}", dst.display())))?;
    fs::create_dir_all(parent).map_err(Error::io)?;

    // Unique temp name in the same directory.
    let mut tmp = parent.to_path_buf();
    let unique = format!(
        ".tmp-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    tmp.push(unique);

    let result = (|| -> Result<u64> {
        let masked = mode & 0o7777 ^ (mode & 0o6000);
        let mut opts = fs::OpenOptions::new();
        opts.write(true)
            .create_new(true)
            .mode(masked)
            .truncate(true);
        let mut f = opts.open(&tmp).map_err(Error::io)?;
        let n = io::copy(&mut reader, &mut f).map_err(Error::io)?;
        f.sync_all().map_err(Error::io)?;
        drop(f);
        chmod(&tmp, mode)?;

        // Replace destination without following it if it's a symlink.
        rename_replace(&tmp, dst)?;
        Ok(n)
    })();

    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Rename `src` over `dst`, replacing any existing non-directory inode and
/// removing a symlink in the way (rename never follows the destination).
fn rename_replace(src: &Path, dst: &Path) -> Result<()> {
    if let Ok(meta) = fs::symlink_metadata(dst) {
        if meta.is_dir() {
            // Cross-type replacement of a directory with a file is driven by
            // the planner (children are whiteouts); at this layer we only see
            // it when the directory is empty.
            fs::remove_dir(dst).map_err(|e| {
                if e.kind() == io::ErrorKind::DirectoryNotEmpty {
                    Error::Conflict(format!(
                        "cannot replace non-empty directory {}",
                        dst.display()
                    ))
                } else {
                    Error::io(e)
                }
            })?;
        } else {
            fs::remove_file(dst).map_err(Error::io)?;
        }
    }
    fs::rename(src, dst).map_err(Error::io)
}

pub fn symlink(target: &str, link_path: &Path) -> Result<()> {
    if let Some(parent) = link_path.parent() {
        fs::create_dir_all(parent).map_err(Error::io)?;
    }
    match std::os::unix::fs::symlink(target, link_path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            let meta = fs::symlink_metadata(link_path).map_err(Error::io)?;
            if meta.is_symlink() || meta.is_file() {
                fs::remove_file(link_path).map_err(Error::io)?;
                std::os::unix::fs::symlink(target, link_path).map_err(Error::io)
            } else {
                Err(Error::Conflict(link_path.display().to_string()))
            }
        }
        Err(e) => Err(Error::io(e)),
    }
}

pub fn hardlink(target: &Path, link_path: &Path) -> Result<()> {
    if let Some(parent) = link_path.parent() {
        fs::create_dir_all(parent).map_err(Error::io)?;
    }
    // Remove an existing inode at the link name first (never follow a symlink).
    if fs::symlink_metadata(link_path).is_ok() {
        remove_any(link_path)?;
    }
    fs::hard_link(target, link_path).map_err(Error::io)
}

/// Read whole file — only used for tiny manifest/config JSON.
pub fn read_all(p: &Path) -> Result<Vec<u8>> {
    let mut f = fs::File::open(p).map_err(Error::io)?;
    let mut out = Vec::new();
    f.read_to_end(&mut out).map_err(Error::io)?;
    Ok(out)
}

/// Write a complete buffer to a fresh file, used by the server for nothing
/// payload-bearing; kept for completeness/debug endpoints.
#[allow(dead_code)]
pub fn write_bytes_atomic(dst: &Path, data: &[u8], mode: u32) -> Result<()> {
    write_file_atomic(dst, mode, data).map(|_| ())
}

/// Ensure `p` is a directory on disk, creating it (and parents) if needed.
/// Does NOT change provenance — that's the planner's responsibility.
pub fn ensure_dir(p: &Path, mode: u32) -> Result<()> {
    if let Ok(meta) = fs::symlink_metadata(p) {
        if meta.is_dir() {
            return chmod(p, mode);
        }
        return Err(Error::Conflict(p.display().to_string()));
    }
    fs::create_dir_all(p).map_err(Error::io)?;
    chmod(p, mode)
}
