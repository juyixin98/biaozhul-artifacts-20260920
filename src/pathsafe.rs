//! Path and link safety.
//!
//! Every path that comes out of a tar entry or whiteout marker passes
//! through [`sanitize_relative`] before touching the filesystem. Symlink
//! and hardlink targets pass through [`check_link_target`].
//!
//! ## Why lexical per-link validation is enough
//!
//! We never resolve links through the filesystem during validation.
//! Instead, when creating a *symlink* we check lexicically that the kernel's
//! resolution of that link, with every previously created link applied,
//! cannot leave the root:
//!
//! * an absolute target is rejected outright;
//! * a relative target is resolved component-by-component against the
//!   link's own directory, applying `..` to the lexical prefix;
//! * if `..` would underflow the root (`../../etc/passwd`, or
//!   `a/../../../x`), the link is rejected.
//!
//! Because each created symlink is individually confined, and parent
//! directories are themselves confined by induction, no chain of
//! `/`-resolution can ever name a path outside the root — a confined link
//! prefix can never produce an unconfined path for the next component to
//! escape through. This is the same argument Docker's own unpacker uses.

use std::path::{Component, Path, PathBuf};

use crate::error::{AppError, AppResult};
use crate::limits::Limits;

/// Clean an archive entry path into a validated, root-relative path.
///
/// Rules:
/// - must be valid UTF-8;
/// - no NUL bytes;
/// - must not be absolute (no leading `/`);
/// - no Windows drive / prefix (`C:` is rejected);
/// - no `..` component;
/// - `.` components are dropped (so `./a/./b` -> `a/b`);
/// - empty path is rejected;
/// - length bounded by `Limits::max_path_len`.
pub fn sanitize_relative(raw: &Path, limits: &Limits) -> AppResult<PathBuf> {
    let s = raw
        .to_str()
        .ok_or_else(|| AppError::rejected(raw.display().to_string(), "non-UTF-8 path"))?;
    if s.contains('\0') {
        return Err(AppError::rejected(s, "NUL byte in path"));
    }
    if s.len() > limits.max_path_len {
        return Err(AppError::limit(format!(
            "entry path longer than {} bytes",
            limits.max_path_len
        )));
    }

    // Reject Windows-style drive prefixes and backslashes. tar entries use
    // `/`; a backslash is treated as an ordinary filename character on Unix,
    // but for fixture safety we reject them to avoid host-dependent surprises.
    if s.contains('\\') {
        return Err(AppError::rejected(s, "backslash in path"));
    }

    let mut out = PathBuf::new();
    for comp in raw.components() {
        match comp {
            Component::Normal(c) => {
                let cs = c
                    .to_str()
                    .ok_or_else(|| AppError::rejected(s, "non-UTF-8 component"))?;
                if cs.is_empty() || cs == "." || cs == ".." {
                    return Err(AppError::rejected(s, "illegal path component"));
                }
                out.push(c);
            }
            Component::CurDir => {} // drop "."
            Component::ParentDir => {
                return Err(AppError::rejected(s, "parent-dir (`..`) component"));
            }
            Component::RootDir | Component::Prefix(_) => {
                return Err(AppError::rejected(s, "absolute path or drive prefix"));
            }
        }
    }
    if out.as_os_str().is_empty() {
        return Err(AppError::rejected(s, "empty path"));
    }
    Ok(out)
}

/// Result of resolving a relative link target lexicically.
enum Lex {
    /// Confined inside the root: normalised relative components.
    Inside(Vec<String>),
    /// Lexically escapes (too many `..`).
    Escapes,
}

/// Resolve `target` (the symlink's content) against the directory
/// containing the link (`link_dir`, root-relative components), returning
/// the normalised root-relative target components, or `Escapes`.
fn resolve_relative(target: &str, link_dir: &[String]) -> Lex {
    // Reject absolute targets and targets containing NUL bytes.
    if target.contains('\0') {
        return Lex::Escapes;
    }
    let t = target.strip_prefix('/');
    let body = match t {
        Some(_) => return Lex::Escapes,
        None => target,
    };
    if body.contains('\\') {
        // Same conservative stance as entry paths.
        return Lex::Escapes;
    }

    let mut stack: Vec<&str> = link_dir.iter().map(|s| s.as_str()).collect();
    for part in body.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if stack.pop().is_none() {
                    return Lex::Escapes;
                }
            }
            other => stack.push(other),
        }
    }
    Lex::Inside(stack.into_iter().map(str::to_string).collect())
}

/// Validate a symlink target for a link located at `link_path`
/// (root-relative, sanitised). Returns the normalised root-relative target
/// path components (the lexical destination under the root).
pub fn check_symlink_target(
    link_path: &Path,
    target: &[u8],
    limits: &Limits,
) -> AppResult<Vec<String>> {
    let target = std::str::from_utf8(target).map_err(|_| {
        AppError::rejected(link_path.display().to_string(), "non-UTF-8 symlink target")
    })?;
    if target.len() > limits.max_path_len {
        return Err(AppError::limit(format!(
            "symlink target longer than {} bytes",
            limits.max_path_len
        )));
    }

    // Directory components of the link itself (a link at `a/b/ln` is
    // resolved relative to `a/b`).
    let mut link_dir: Vec<String> = Vec::new();
    {
        let comps: Vec<_> = link_path.components().collect();
        // link_path is sanitised relative: all components are Normal.
        for c in &comps[..comps.len().saturating_sub(1)] {
            if let Component::Normal(n) = c {
                link_dir.push(n.to_string_lossy().into_owned());
            }
        }
    }

    match resolve_relative(target, &link_dir) {
        Lex::Inside(v) => Ok(v),
        Lex::Escapes => Err(AppError::rejected(
            link_path.display().to_string(),
            format!("symlink target escapes root: {target}"),
        )),
    }
}

/// Validate a hardlink target (tar `Linkname` for a hard link entry).
/// tar hardlinks use archive-relative paths (never symlink content), so the
/// target must simply be a sanitised in-root path.
pub fn check_hardlink_target(
    raw_target: &Path,
    link_path: &Path,
    limits: &Limits,
) -> AppResult<PathBuf> {
    sanitize_relative(raw_target, limits).map_err(|_| {
        AppError::rejected(
            link_path.display().to_string(),
            format!("hardlink target escapes root: {}", raw_target.display()),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lim() -> Limits {
        Limits::default()
    }

    #[test]
    fn accepts_plain_and_dot_relative() {
        assert_eq!(
            sanitize_relative(Path::new("a/b/c"), &lim()).unwrap(),
            PathBuf::from("a/b/c")
        );
        assert_eq!(
            sanitize_relative(Path::new("./a/./b"), &lim()).unwrap(),
            PathBuf::from("a/b")
        );
    }

    #[test]
    fn rejects_traversal_and_absolute() {
        assert!(sanitize_relative(Path::new("../x"), &lim()).is_err());
        assert!(sanitize_relative(Path::new("a/../../x"), &lim()).is_err());
        assert!(sanitize_relative(Path::new("/etc/passwd"), &lim()).is_err());
        assert!(sanitize_relative(Path::new("a\\b"), &lim()).is_err());
        assert!(sanitize_relative(Path::new(""), &lim()).is_err());
    }

    #[test]
    fn symlink_confinement_matrix() {
        // link at root, relative target staying inside
        let r = check_symlink_target(Path::new("ln"), b"a/b", &lim()).unwrap();
        assert_eq!(r, vec!["a".to_string(), "b".to_string()]);

        // self-link dir ./a/ln -> ../b  => a/b (inside)
        let r = check_symlink_target(Path::new("a/ln"), b"../b", &lim()).unwrap();
        assert_eq!(r, vec!["b".to_string()]);

        // too many parents -> escape
        assert!(check_symlink_target(Path::new("ln"), b"../../x", &lim()).is_err());
        assert!(check_symlink_target(Path::new("a/ln"), b"../../x", &lim()).is_err());
        // absolute -> escape
        assert!(check_symlink_target(Path::new("ln"), b"/etc", &lim()).is_err());
        // dotdot chain that lands back inside
        let r = check_symlink_target(Path::new("a/b/ln"), b"../../a/c", &lim()).unwrap();
        assert_eq!(r, vec!["a".to_string(), "c".to_string()]);
    }
}
