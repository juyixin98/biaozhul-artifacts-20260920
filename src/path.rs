//! Relative path and symlink-target normalization.

use std::fmt;

/// A validated, normalized relative path inside the output root.
///
/// Stored as non-empty path components. Absolute paths, `.`/`..` traversal,
/// empty components and NUL bytes are all rejected.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RelPath(pub Vec<String>);

/// Why an input path or link target was rejected (mapped to HTTP 400).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PathError {
    Empty,
    Absolute(String),
    EmptyComponent(String),
    ParentTraversal(String),
    CurrentDir(String),
    NulByte(String),
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PathError::Empty => write!(f, "path is empty"),
            PathError::Absolute(p) => write!(f, "path must be relative, got absolute path: {p:?}"),
            PathError::EmptyComponent(p) => {
                write!(
                    f,
                    "path contains an empty component (double/trailing slash): {p:?}"
                )
            }
            PathError::ParentTraversal(p) => {
                write!(f, "path must not contain '..': {p:?}")
            }
            PathError::CurrentDir(p) => write!(f, "path must not be '.': {p:?}"),
            PathError::NulByte(p) => write!(f, "path contains a NUL byte: {p:?}"),
        }
    }
}

impl std::error::Error for PathError {}

impl RelPath {
    /// Parse a client-supplied path.
    pub fn parse(raw: &str) -> Result<RelPath, PathError> {
        if raw.is_empty() {
            return Err(PathError::Empty);
        }
        if raw.contains('\0') {
            return Err(PathError::NulByte(raw.to_string()));
        }
        // Leading '/' makes the path absolute. Trailing/duplicate '/' yields
        // empty components. Backslashes are kept as ordinary file-name chars
        // (the service targets POSIX roots); see README "语义与边界".
        if raw.starts_with('/') {
            return Err(PathError::Absolute(raw.to_string()));
        }
        let comps: Vec<&str> = raw.split('/').collect();
        for c in &comps {
            match *c {
                "" => return Err(PathError::EmptyComponent(raw.to_string())),
                "." => return Err(PathError::CurrentDir(raw.to_string())),
                ".." => return Err(PathError::ParentTraversal(raw.to_string())),
                _ => {}
            }
        }
        Ok(RelPath(comps.into_iter().map(str::to_string).collect()))
    }

    pub fn as_str(&self) -> String {
        self.0.join("/")
    }

    /// Case-folded components used for case-insensitive collision detection.
    pub fn folded(&self) -> Vec<String> {
        self.0.iter().map(|c| c.to_lowercase()).collect()
    }

    /// `/`-joined folded components (a key in the folded index).
    pub fn folded_str(&self) -> String {
        self.0
            .iter()
            .map(|c| c.to_lowercase())
            .collect::<Vec<_>>()
            .join("/")
    }
}

impl fmt::Display for RelPath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.as_str())
    }
}

/// A normalized symlink target.
///
/// `escapes_root` is decided by pure lexical resolution of the relative target
/// against its link's parent directory: if more `..` steps leave the tree than
/// named ancestors exist, the link can point outside the output root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkTarget {
    pub raw: String,
    pub normalized: String,
    pub absolute: bool,
    pub escapes_root: bool,
}

pub fn normalize_target(raw: &str, link_path: &RelPath) -> Result<LinkTarget, PathError> {
    if raw.is_empty() {
        return Err(PathError::Empty);
    }
    if raw.contains('\0') {
        return Err(PathError::NulByte(raw.to_string()));
    }
    let absolute = raw.starts_with('/');
    // Components of the target, dropping "." and "" (// allowed, trailing /
    // allowed) but keeping "..".
    let comps: Vec<&str> = raw
        .split('/')
        .filter(|c| !c.is_empty() && *c != ".")
        .collect();
    // Depth of the link's parent directory from the output root:
    // `x/y/link` lives at depth 2 -> its parent can consume two '..' steps
    // before reaching the root, and a third escapes.
    let mut depth: i64 = link_path.0.len() as i64 - 1;
    let mut escapes = absolute;
    let mut stack: Vec<&str> = Vec::new();
    for c in &comps {
        if *c == ".." {
            depth -= 1;
            if depth < 0 {
                escapes = true;
            }
            stack.pop();
        } else {
            stack.push(c);
            depth += 1;
        }
    }
    let normalized = if absolute {
        format!("/{}", stack.join("/"))
    } else {
        stack.join("/")
    };
    Ok(LinkTarget {
        raw: raw.to_string(),
        normalized,
        absolute,
        escapes_root: escapes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_relative_paths() {
        assert_eq!(RelPath::parse("a/b/c").unwrap().0, ["a", "b", "c"]);
        assert!(RelPath::parse("a").is_ok());
    }

    #[test]
    fn rejects_bad_paths() {
        assert!(matches!(RelPath::parse(""), Err(PathError::Empty)));
        assert!(matches!(RelPath::parse("/a"), Err(PathError::Absolute(_))));
        assert!(matches!(
            RelPath::parse("a//b"),
            Err(PathError::EmptyComponent(_))
        ));
        assert!(matches!(
            RelPath::parse("a/"),
            Err(PathError::EmptyComponent(_))
        ));
        assert!(matches!(
            RelPath::parse("a/../b"),
            Err(PathError::ParentTraversal(_))
        ));
        assert!(matches!(RelPath::parse("."), Err(PathError::CurrentDir(_))));
        assert!(matches!(RelPath::parse("a\0b"), Err(PathError::NulByte(_))));
    }

    #[test]
    fn link_target_escape_detection() {
        let link = RelPath::parse("x/y/link").unwrap();
        assert!(!normalize_target("../z", &link).unwrap().escapes_root);
        assert!(
            normalize_target("../../../etc", &link)
                .unwrap()
                .escapes_root
        );
        assert!(normalize_target("/etc/passwd", &link).unwrap().escapes_root);
        assert!(normalize_target("/etc/passwd", &link).unwrap().absolute);
        // target equal to link's own dir is still inside the tree
        assert!(!normalize_target("..", &link).unwrap().escapes_root);
        // root-level link cannot use any '..'
        let root_link = RelPath::parse("link").unwrap();
        assert!(normalize_target("..", &root_link).unwrap().escapes_root);
        // dot and duplicate slashes normalized (path kept relative to the
        // link's own directory; the prefix is not expanded)
        assert_eq!(
            normalize_target("./a//b/", &link).unwrap().normalized,
            "a/b"
        );
    }
}
