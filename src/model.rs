//! Core domain model shared by the planner, applier and HTTP layer.

use crate::path::RelPath;

/// Kind of a single build output.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EntryKind {
    File,
    Dir,
    Symlink,
}

impl EntryKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EntryKind::File => "file",
            EntryKind::Dir => "dir",
            EntryKind::Symlink => "symlink",
        }
    }
}

/// One output declared by a build action.
#[derive(Debug, Clone)]
pub struct OutputSpec {
    pub path: RelPath,
    pub kind: EntryKind,
    /// File bytes (kind == file only).
    pub content: Option<Vec<u8>>,
    /// Lowercase hex SHA-256 of `content` (files only).
    pub content_hash: Option<String>,
    /// Link target, raw string (kind == symlink only).
    pub link_target: Option<String>,
}

/// Provenance: which actions produced an output at a given path, and at what
/// index inside that action's output list (0-based).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Prov {
    pub action: String,
    pub index: usize,
}

/// Why a group of outputs is in conflict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConflictKind {
    /// Two outputs land on the same path with incompatible kinds
    /// (e.g. file vs dir), or same kind but different bytes/link targets.
    SamePathIncompatible,
    /// A file/symlink is an ancestor of another output
    /// (classic "file `a` vs directory `a/b`").
    AncestorBlocking,
    /// Two paths differ only by case, at the same depth.
    CaseFoldCollision,
    /// As `AncestorBlocking`, but the ancestor only matches when case-folded.
    CaseFoldAncestor,
    /// A symlink target escapes the output root (`..` breakout or absolute).
    UnsafeLinkTarget,
}

impl ConflictKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ConflictKind::SamePathIncompatible => "same_path_incompatible",
            ConflictKind::AncestorBlocking => "ancestor_blocking",
            ConflictKind::CaseFoldCollision => "case_fold_collision",
            ConflictKind::CaseFoldAncestor => "case_fold_ancestor",
            ConflictKind::UnsafeLinkTarget => "unsafe_link_target",
        }
    }
}

/// One detected conflict. `path` is the shortest conflict path (see README),
/// `other_paths` lists the other paths involved.
#[derive(Debug, Clone)]
pub struct Conflict {
    pub kind: ConflictKind,
    pub path: String,
    pub other_paths: Vec<String>,
    pub actions: Vec<String>,
    pub detail: String,
}

/// Final node of the merged tree as planned (before applying).
#[derive(Debug, Clone)]
pub struct PlannedNode {
    pub path: RelPath,
    pub kind: EntryKind,
    pub actions: Vec<Prov>,
    /// Files: all contributing actions agree on these bytes.
    pub content: Option<Vec<u8>>,
    pub content_hash: Option<String>,
    pub link_target: Option<String>,
}

/// A group of distinct paths carrying byte-identical file content
/// (dedup candidates; the applier stores one blob and hard-links).
#[derive(Debug, Clone)]
pub struct SharedContent {
    pub content_hash: String,
    pub paths: Vec<String>,
    pub actions: Vec<String>,
    pub byte_len: usize,
}

/// The complete merge plan. Nothing in here touches the filesystem.
#[derive(Debug, Clone)]
pub struct Plan {
    pub nodes: Vec<PlannedNode>,
    pub conflicts: Vec<Conflict>,
    pub shared: Vec<SharedContent>,
    pub action_ids: Vec<String>,
}
