//! In-memory tree model used by the builder, parsed from JSON control input.

use crate::error::{IfixError, Result};
use crate::format::Limits;
use std::io::Read;

/// Blob payload for a file node.
#[derive(Debug, Clone)]
pub enum Blob {
    /// No bytes (empty file).
    Empty,
    /// Bytes embedded in the JSON request as raw bytes (decoded from base64).
    Inline(Vec<u8>),
    /// Path resolved through a [`BlobResolver`] while writing, streamed to disk.
    File(String),
}

/// One tree node supplied by the caller.
#[derive(Debug, Clone)]
pub struct InputNode {
    pub name: String,
    pub is_dir: bool,
    pub blob: Blob,
    pub children: Vec<InputNode>,
}

impl InputNode {
    /// Parse a node from a JSON object:
    /// `{"name": "...", "type": "file"|"dir",
    ///   "content_b64": "...", "content_file": "...",
    ///   "children": [ ... ]}`
    pub fn from_json(value: &serde_json::Value) -> Result<Self> {
        let obj = value
            .as_object()
            .ok_or_else(|| IfixError::Json("each node must be a JSON object".into()))?;

        let name = obj
            .get("name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| IfixError::Json("node requires string field \"name\"".into()))?
            .to_owned();

        let ty = obj
            .get("type")
            .and_then(|v| v.as_str())
            .ok_or_else(|| IfixError::Json("node requires string field \"type\"".into()))?;
        let is_dir = match ty {
            "dir" | "directory" => true,
            "file" => false,
            other => {
                return Err(IfixError::Json(format!(
                    "node \"{name}\": type must be \"file\" or \"dir\", got {other:?}"
                )))
            }
        };

        if obj.contains_key("content_b64") && obj.contains_key("content_file") {
            return Err(IfixError::Json(format!(
                "node \"{name}\": content_b64 and content_file are mutually exclusive"
            )));
        }

        let mut blob = Blob::Empty;
        if let Some(s) = obj.get("content_b64").and_then(|v| v.as_str()) {
            blob = Blob::Inline(crate::base64::decode(s)?);
        }
        if let Some(s) = obj.get("content_file").and_then(|v| v.as_str()) {
            if s.is_empty() {
                return Err(IfixError::Json(format!(
                    "node \"{name}\": content_file is empty"
                )));
            }
            blob = Blob::File(s.to_owned());
        }

        let children = match obj.get("children") {
            None | Some(serde_json::Value::Null) => Vec::new(),
            Some(serde_json::Value::Array(a)) => {
                let mut out = Vec::with_capacity(a.len());
                for child in a {
                    out.push(InputNode::from_json(child)?);
                }
                out
            }
            Some(_) => {
                return Err(IfixError::Json(format!(
                    "node \"{name}\": \"children\" must be an array"
                )))
            }
        };

        if is_dir && !matches!(blob, Blob::Empty) {
            return Err(IfixError::Json(format!(
                "node \"{name}\": directory nodes cannot carry content"
            )));
        }
        if !is_dir && !children.is_empty() {
            return Err(IfixError::Json(format!(
                "node \"{name}\": file nodes cannot have children"
            )));
        }
        if name.contains('/') || name.contains('\0') {
            return Err(IfixError::Json(format!(
                "node name {name:?} must not contain '/' or NUL"
            )));
        }
        if name == "." || name == ".." {
            return Err(IfixError::Json(format!("node name {name:?} is reserved")));
        }

        Ok(InputNode {
            name,
            is_dir,
            blob,
            children,
        })
    }
}

/// A build request: the tree plus builder parameters.
#[derive(Debug)]
pub struct BuildRequest {
    pub root: InputNode,
    pub log2_page: u8,
}

impl BuildRequest {
    pub fn from_json(value: &serde_json::Value) -> Result<Self> {
        let obj = value
            .as_object()
            .ok_or_else(|| IfixError::Json("build request must be a JSON object".into()))?;
        let root_value = obj
            .get("root")
            .ok_or_else(|| IfixError::Json("build request requires \"root\"".into()))?;
        let root = InputNode::from_json(root_value)?;
        if !root.is_dir {
            return Err(IfixError::Json("root node must be a directory".into()));
        }
        let log2_page = match obj.get("log2_page") {
            None | Some(serde_json::Value::Null) => 8,
            Some(v) => v
                .as_u64()
                .ok_or_else(|| IfixError::Json("log2_page must be an integer".into()))?
                as u8,
        };
        Ok(BuildRequest { root, log2_page })
    }
}

/// Resolves `content_file` references to readable streams.
pub trait BlobResolver {
    fn open(&self, path: &str) -> Result<Box<dyn Read>>;
}

/// Resolves files relative to a base directory, refusing absolute paths
/// and path traversal outside that base.
pub struct FileResolver {
    base: std::path::PathBuf,
}

impl FileResolver {
    pub fn new(base: impl Into<std::path::PathBuf>) -> Self {
        FileResolver { base: base.into() }
    }
}

impl BlobResolver for FileResolver {
    fn open(&self, path: &str) -> Result<Box<dyn Read>> {
        let p = std::path::Path::new(path);
        if p.is_absolute() {
            return Err(IfixError::Json(format!(
                "content_file must be relative to the request base directory: {path:?}"
            )));
        }
        let mut depth: i32 = 0;
        for part in p.components() {
            use std::path::Component;
            match part {
                Component::Normal(_) => depth += 1,
                Component::CurDir => {}
                Component::ParentDir => depth -= 1,
                _ => {
                    return Err(IfixError::Json(format!(
                        "content_file has unsupported path component: {path:?}"
                    )))
                }
            }
            if depth < 0 {
                return Err(IfixError::Json(format!(
                    "content_file escapes the request base directory: {path:?}"
                )));
            }
        }
        let full = self.base.join(p);
        let f = std::fs::File::open(&full).map_err(|e| {
            IfixError::Json(format!("cannot open content_file {}: {e}", full.display()))
        })?;
        Ok(Box::new(f))
    }
}

/// Bounded recursive validation of an input tree. Returns the total number
/// of nodes and the total blob size. Sorts sibling lists lexicographically so
/// the on-disk layout permits binary search during lookup.
pub struct PreparedTree {
    pub root: InputNode,
    pub node_count: u32,
    pub data_bytes: u64,
}

pub fn prepare(mut root: InputNode, limits: &Limits) -> Result<PreparedTree> {
    if !root.name.is_empty() {
        return Err(IfixError::Json("root node name must be empty".into()));
    }
    let mut node_count = 0u32;
    let mut data_bytes = 0u64;
    prepare_rec(&mut root, 0, limits, &mut node_count, &mut data_bytes)?;
    Ok(PreparedTree {
        root,
        node_count,
        data_bytes,
    })
}

fn prepare_rec(
    node: &mut InputNode,
    depth: u32,
    limits: &Limits,
    count: &mut u32,
    data_bytes: &mut u64,
) -> Result<()> {
    if depth > limits.max_depth {
        return Err(IfixError::format(
            "LIMIT_DEPTH",
            format!("path depth exceeds {}", limits.max_depth),
        ));
    }
    limits.check_name_len(node.name.len())?;
    if std::str::from_utf8(node.name.as_bytes()).is_err() {
        return Err(IfixError::Json(format!(
            "node name {:?} is not valid UTF-8",
            node.name
        )));
    }

    *count = count
        .checked_add(1)
        .ok_or_else(|| IfixError::format("LIMIT_NODES", "node count overflow while counting"))?;
    if *count > limits.max_nodes {
        return Err(IfixError::format(
            "LIMIT_NODES",
            format!("node count exceeds configured limit {}", limits.max_nodes),
        ));
    }

    if node.is_dir {
        node.children.sort_by(|a, b| a.name.cmp(&b.name));
        let mut seen = std::collections::HashSet::new();
        for child in &node.children {
            if !seen.insert(&child.name) {
                return Err(IfixError::Json(format!(
                    "duplicate sibling name {:?} under {:?}",
                    child.name, node.name
                )));
            }
        }
        for child in &mut node.children {
            prepare_rec(child, depth + 1, limits, count, data_bytes)?;
        }
    } else {
        let inline_len = match &node.blob {
            Blob::Empty => 0u64,
            Blob::Inline(v) => v.len() as u64,
            // File size is checked while streaming in the writer.
            Blob::File(_) => 0,
        };
        *data_bytes = data_bytes
            .checked_add(inline_len)
            .ok_or_else(|| IfixError::format("LIMIT_DATA", "data size overflow while counting"))?;
    }
    // Names also live in the data region.
    *data_bytes = data_bytes
        .checked_add(node.name.len() as u64)
        .ok_or_else(|| {
            IfixError::format("LIMIT_DATA", "data size overflow while counting names")
        })?;
    if *data_bytes > limits.max_data_bytes {
        return Err(IfixError::format(
            "LIMIT_DATA",
            format!(
                "data region exceeds configured limit {} bytes",
                limits.max_data_bytes
            ),
        ));
    }
    Ok(())
}
