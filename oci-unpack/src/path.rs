//! Path safety: every path coming out of a tar header is treated as hostile.
//!
//! Policy:
//! * entry names must be relative, UTF-8, and contain no `..` component;
//! * absolute symlink targets are rejected (they would point at host paths);
//! * relative symlink targets are resolved lexically and must stay inside the
//!   rootfs (dangling-but-contained links are allowed);
//! * regular writes additionally verify that no ancestor directory is itself a
//!   symlink, so a link created by an earlier layer can never be used to write
//!   through (defence in depth on top of the lexical link check).

use std::path::{Component, Path, PathBuf};

use crate::error::{Error, Result};
use crate::limits::Limits;

/// A normalized, root-relative path: no leading slash, no `.`/`..`/empty
/// components, UTF-8. Built only through [`normalize_entry_name`].
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RelPath {
    components: Vec<String>,
}

impl RelPath {
    /// Rebuild from already-validated components (e.g. provenance keys).
    pub fn from_components(components: Vec<String>) -> Self {
        RelPath { components }
    }

    pub fn components(&self) -> &[String] {
        &self.components
    }

    /// `a/b/c` style string used as the provenance-map key.
    pub fn as_str(&self) -> String {
        self.components.join("/")
    }

    pub fn parent_components(&self) -> &[String] {
        if self.components.is_empty() {
            &[]
        } else {
            &self.components[..self.components.len() - 1]
        }
    }

    pub fn file_name(&self) -> Option<&str> {
        self.components.last().map(String::as_str)
    }

    pub fn depth(&self) -> usize {
        self.components.len()
    }

    /// True if `self` is `prefix` itself or a descendant of it.
    pub fn is_within(&self, prefix: &[String]) -> bool {
        self.components.len() >= prefix.len() && self.components[..prefix.len()] == *prefix
    }

    /// Strict-descendant test (excludes the prefix itself).
    pub fn is_strictly_within(&self, prefix: &RelPath) -> bool {
        self.components.len() > prefix.components.len()
            && self.components[..prefix.components.len()] == prefix.components[..]
    }

    /// Joins the path onto a root using only pushed components, so even a
    /// compromised component string can never smuggle an absolute path.
    pub fn under(&self, root: &Path) -> PathBuf {
        let mut p = root.to_path_buf();
        for c in &self.components {
            p.push(c);
        }
        p
    }
}

/// Normalize a raw tar entry name into a [`RelPath`].
pub fn normalize_entry_name(raw: &[u8], limits: &Limits) -> Result<RelPath> {
    if raw.is_empty() {
        return Err(Error::corrupt("empty entry name"));
    }
    if raw.len() > 4096 {
        return Err(Error::PathTraversal(
            String::from_utf8_lossy(raw).into_owned(),
        ));
    }
    if raw.contains(&0u8) {
        return Err(Error::corrupt("entry name contains NUL byte"));
    }
    let s = std::str::from_utf8(raw)
        .map_err(|_| Error::PathTraversal(String::from_utf8_lossy(raw).into_owned()))?;

    // Reject Windows-style drive prefixes / UNC and backslashes outright: on a
    // Linux extractor they are legal filename characters, but in OCI layers
    // they only ever appear in hand-crafted malicious archives.
    if s.contains('\\') || s.contains(':') {
        return Err(Error::PathTraversal(s.to_string()));
    }
    if s.starts_with('/') {
        return Err(Error::PathTraversal(s.to_string()));
    }

    let mut components: Vec<String> = Vec::new();
    for comp in s.split('/') {
        match comp {
            "" | "." => {
                // tolerate leading "./" and duplicated slashes by skipping
            }
            ".." => return Err(Error::PathTraversal(s.to_string())),
            c => {
                if c.len() > 255 {
                    return Err(Error::PathTraversal(s.to_string()));
                }
                components.push(c.to_string());
            }
        }
    }
    if components.is_empty() {
        return Err(Error::corrupt("entry name resolves to root"));
    }
    if components.iter().map(|c| c.len() + 1).sum::<usize>() > limits.max_link_name_len * 8 {
        return Err(Error::limit(format!("entry path too long: {s}")));
    }
    Ok(RelPath { components })
}

/// Result of resolving a symlink target lexically against the link's parent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainedTarget {
    pub components: Vec<String>,
}

/// Validate a symlink target: UTF-8, relative, lexically contained in root.
///
/// `link` is the path of the symlink itself; the target is interpreted
/// relative to the link's parent directory.
pub fn validate_symlink_target(
    link: &RelPath,
    raw_target: &[u8],
    limits: &Limits,
) -> Result<ContainedTarget> {
    if raw_target.is_empty() {
        return Err(Error::corrupt(format!(
            "empty symlink target at {}",
            link.as_str()
        )));
    }
    if raw_target.len() > limits.max_link_name_len {
        return Err(Error::limit(format!(
            "symlink target length {} exceeds limit {}",
            raw_target.len(),
            limits.max_link_name_len
        )));
    }
    if raw_target.contains(&0u8) {
        return Err(Error::corrupt("symlink target contains NUL byte"));
    }
    let target = std::str::from_utf8(raw_target).map_err(|_| Error::LinkEscape {
        link: link.as_str(),
        target: String::from_utf8_lossy(raw_target).into_owned(),
    })?;
    if target.starts_with('/') {
        return Err(Error::LinkEscape {
            link: link.as_str(),
            target: target.to_string(),
        });
    }
    if target.contains('\\') {
        return Err(Error::LinkEscape {
            link: link.as_str(),
            target: target.to_string(),
        });
    }

    // Walk the target from the link parent's depth. Depth is an unsigned
    // counter that is only ever popped down to zero; a `..` at zero escapes.
    let parent = link.parent_components();
    let mut resolved: Vec<&str> = parent.iter().map(String::as_str).collect();
    for comp in target.split('/') {
        match comp {
            "" | "." => {}
            ".." => {
                if resolved.pop().is_none() {
                    return Err(Error::LinkEscape {
                        link: link.as_str(),
                        target: target.to_string(),
                    });
                }
            }
            c => {
                if c.len() > 255 {
                    return Err(Error::limit("symlink path component too long"));
                }
                resolved.push(c);
            }
        }
    }
    Ok(ContainedTarget {
        components: resolved.iter().map(|s| s.to_string()).collect(),
    })
}

/// Verify that no ancestor of `rel` is a symlink in `symlinks`.
/// `symlinks` contains the normalized symlink paths accumulated from all
/// layers applied so far.
pub fn assert_no_symlink_ancestor(
    rel: &RelPath,
    symlinks: &std::collections::BTreeSet<String>,
) -> Result<()> {
    let comps = rel.components();
    let mut acc = String::new();
    for c in &comps[..comps.len().saturating_sub(1)] {
        if !acc.is_empty() {
            acc.push('/');
        }
        acc.push_str(c);
        if symlinks.contains(&acc) {
            return Err(Error::AncestorIsSymlink(rel.as_str()));
        }
    }
    Ok(())
}

/// Resolve a hard-link target name (always relative to root) and require it to
/// be lexically contained.
pub fn normalize_link_target(raw: &[u8], limits: &Limits) -> Result<RelPath> {
    let p = normalize_entry_name(raw, limits)?;
    // Hard-link targets must not be absolute (already rejected) and we also
    // forbid targeting through a symlink at extraction time via the same
    // ancestor check as regular entries.
    Ok(p)
}

/// Defence-in-depth check after joining: canonicalize the *root* once and
/// verify the joined parent has not escaped through pre-existing filesystem
/// state. Best-effort: if the path does not exist yet the lexical checks
/// above remain authoritative.
pub fn assert_joined_inside_root(root: &Path, joined: &Path) -> Result<()> {
    if joined == root {
        return Ok(());
    }
    let canon_root = root
        .canonicalize()
        .map_err(|e| Error::io(format!("canonicalize root: {e}")))?;
    let parent = joined.parent().unwrap_or(root);
    if let Ok(canon_parent) = parent.canonicalize() {
        if !is_prefixed_componentwise(&canon_root, &canon_parent) && canon_parent != canon_root {
            return Err(Error::PathTraversal(joined.display().to_string()));
        }
    }
    // Also verify the joined path itself was built only from Normal components.
    for comp in joined.components() {
        if matches!(
            comp,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        ) {
            return Err(Error::PathTraversal(joined.display().to_string()));
        }
    }
    Ok(())
}

fn is_prefixed_componentwise(root: &Path, candidate: &Path) -> bool {
    let r: Vec<_> = root.components().collect();
    let c: Vec<_> = candidate.components().collect();
    c.starts_with(&r)
}
