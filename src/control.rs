//! JSON control entry point.
//!
//! Each command takes one JSON request value and returns one JSON response
//! value, wrapped in a uniform envelope by the CLI:
//! `{"status":"ok","data":...}` or `{"status":"error","code":"...","message":"..."}`.

use crate::error::{IfixError, Result};
use crate::format::Limits;
use crate::model::{prepare, BuildRequest, FileResolver};
use crate::reader::IndexFile;
use crate::storage::{FileStorage, MemStorage, MmapStorage};
use crate::writer::write_index;
use serde_json::{json, Value};
use std::io::{Cursor, Read};

/// Which storage backend serves reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    /// `pread`-style reads on a plain file descriptor.
    Read,
    /// Private read-only `mmap`.
    Mmap,
}

fn field<'a>(req: &'a Value, name: &str) -> Result<&'a str> {
    req.get(name)
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| IfixError::Json(format!("command requires non-empty string field {name:?}")))
}

fn optional_backend(req: &Value) -> Result<Backend> {
    match req.get("backend").and_then(|v| v.as_str()) {
        None | Some("read") | Some("file") => Ok(Backend::Read),
        Some("mmap") => Ok(Backend::Mmap),
        Some(other) => Err(IfixError::Json(format!(
            "unknown backend {other:?} (read|mmap)"
        ))),
    }
}

fn limits_from_request(req: &Value) -> Limits {
    // The CLI starts from Default; callers may only tighten via a nested
    // "limits" object. Unknown keys are ignored.
    let mut limits = Limits::default();
    if let Some(obj) = req.get("limits").and_then(|v| v.as_object()) {
        if let Some(v) = obj.get("max_output_bytes").and_then(|v| v.as_u64()) {
            limits.max_output_bytes = v;
        }
        if let Some(v) = obj.get("max_depth").and_then(|v| v.as_u64()) {
            limits.max_depth = v.min(u32::MAX as u64) as u32;
        }
        if let Some(v) = obj.get("max_nodes").and_then(|v| v.as_u64()) {
            limits.max_nodes = v.min(u32::MAX as u64) as u32;
        }
    }
    limits
}

/// Dispatch one parsed JSON command.
pub fn dispatch(req: &Value, base_dir: &std::path::Path) -> Result<Value> {
    let cmd = field(req, "command")?;
    match cmd {
        "build" => build(req, base_dir),
        "lookup" => lookup(req),
        "list" => list(req),
        "read" | "cat" => read_blob(req),
        "verify" => verify(req),
        "stats" => stats(req),
        other => Err(IfixError::Json(format!(
            "unknown command {other:?} (build|lookup|list|read|verify|stats)"
        ))),
    }
}

fn build(req: &Value, base_dir: &std::path::Path) -> Result<Value> {
    let out_path = field(req, "output")?;
    let limits = limits_from_request(req);
    let build_req = BuildRequest::from_json(req)?;
    let prepared = prepare(build_req.root.clone(), &limits)?;

    let mut file = std::fs::File::create(out_path)
        .map_err(|e| IfixError::format("IO", format!("cannot create {out_path}: {e}")))?;
    let resolver = FileResolver::new(base_dir);
    let s = write_index(&build_req, &prepared, &resolver, &mut file, &limits)?;

    Ok(json!({
        "path": out_path,
        "file_len": s.file_len,
        "node_count": s.node_count,
        "data_bytes": s.data_bytes,
        "toc_levels": s.toc_levels,
    }))
}

fn open_index(req: &Value) -> Result<IndexHandle> {
    let path = field(req, "index")?;
    let limits = limits_from_request(req);
    match optional_backend(req)? {
        Backend::Read => {
            let f = IndexFile::open(FileStorage::open(path)?, limits)?;
            Ok(IndexHandle::Read(f))
        }
        Backend::Mmap => {
            let f = IndexFile::open(MmapStorage::open(path)?, limits)?;
            Ok(IndexHandle::Mmap(f))
        }
    }
}

enum IndexHandle {
    Read(IndexFile<FileStorage>),
    Mmap(IndexFile<MmapStorage>),
}

macro_rules! with_index {
    ($handle:expr, |$idx:ident| $body:expr) => {
        match $handle {
            IndexHandle::Read($idx) => $body,
            IndexHandle::Mmap($idx) => $body,
        }
    };
}

fn lookup(req: &Value) -> Result<Value> {
    let path = field(req, "path")?;
    let handle = open_index(req)?;
    with_index!(handle, |idx| {
        let meta = idx.lookup(path)?;
        Ok(node_meta_json(&meta))
    })
}

fn list(req: &Value) -> Result<Value> {
    let path = field(req, "path")?;
    let handle = open_index(req)?;
    with_index!(handle, |idx| {
        let entries = idx.list(path)?;
        Ok(json!({
            "path": path,
            "entries": entries.iter().map(node_meta_json).collect::<Vec<_>>(),
        }))
    })
}

fn read_blob(req: &Value) -> Result<Value> {
    let path = field(req, "path")?;
    let max_b64 = req
        .get("max_inline_base64")
        .and_then(|v| v.as_u64())
        .unwrap_or(64 * 1024);
    let handle = open_index(req)?;
    with_index!(handle, |idx| {
        let meta = idx.lookup(path)?;
        if meta.is_dir {
            return Err(IfixError::format(
                "NOT_A_FILE",
                format!("{path:?} is a directory"),
            ));
        }
        // Output length is bounded twice: IndexFile enforces max_output_bytes
        // while streaming; here the collected vector is also capped.
        if meta.size > max_b64 {
            return Err(IfixError::format(
                "LIMIT_OUTPUT",
                format!(
                    "blob is {} bytes; refusing to inline more than {max_b64} (raise max_inline_base64)",
                    meta.size
                ),
            ));
        }
        let mut buf = Cursor::new(Vec::with_capacity(meta.size as usize));
        let n = idx.read_blob(path, &mut buf)?;
        Ok(json!({
            "path": path,
            "size": n,
            "content_b64": crate::base64::encode(buf.get_ref()),
        }))
    })
}

fn verify(req: &Value) -> Result<Value> {
    let path = field(req, "index")?;
    let handle = open_index(req)?;
    with_index!(handle, |idx| {
        Ok(json!({
            "index": path,
            "ok": true,
            "node_count": idx.node_count(),
            "file_len": idx.file_len(),
            "toc_height": idx.toc_height(),
        }))
    })
}

fn stats(req: &Value) -> Result<Value> {
    let path = field(req, "index")?;
    let handle = open_index(req)?;
    with_index!(handle, |idx| {
        Ok(json!({
            "index": path,
            "node_count": idx.node_count(),
            "file_len": idx.file_len(),
            "data_bytes": idx.data_bytes(),
            "nodes_offset": idx.nodes_offset(),
            "toc_offset": idx.toc_offset(),
            "toc_height": idx.toc_height(),
        }))
    })
}

fn node_meta_json(meta: &crate::reader::NodeMeta) -> Value {
    json!({
        "id": meta.id,
        "name": meta.name,
        "type": if meta.is_dir { "dir" } else { "file" },
        "size": meta.size,
        "child_count": meta.child_count,
    })
}

/// Read a JSON request stream with a hard byte cap, returning the parsed
/// value together with any trailing bytes (used by tests/diagnostics).
pub fn read_request<R: Read>(input: &mut R, limits: &Limits) -> Result<Value> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8 * 1024];
    loop {
        let n = input.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        if buf.len() as u64 + n as u64 > limits.max_input_bytes {
            return Err(IfixError::format(
                "LIMIT_INPUT",
                format!("request exceeds {} bytes", limits.max_input_bytes),
            ));
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let value: Value = serde_json::from_slice(&buf)?;
    Ok(value)
}

/// Wrap a command outcome in the standard response envelope, serialized.
pub fn envelope(result: Result<Value>) -> Value {
    match result {
        Ok(data) => json!({ "status": "ok", "data": data }),
        Err(err) => json!({
            "status": "error",
            "code": err.code(),
            "message": err.to_string(),
        }),
    }
}

/// Convenience used by integration tests: build in memory from a tree JSON.
pub fn build_to_memory(request_json: &str, limits: &Limits) -> Result<Vec<u8>> {
    let req_value: Value = serde_json::from_str(request_json)?;
    let build_req = BuildRequest::from_json(&req_value)?;
    let prepared = prepare(build_req.root.clone(), limits)?;
    let resolver = FailingResolver;
    let mut cursor = Cursor::new(Vec::new());
    write_index(&build_req, &prepared, &resolver, &mut cursor, limits)?;
    Ok(cursor.into_inner())
}

struct FailingResolver;
impl crate::model::BlobResolver for FailingResolver {
    fn open(&self, _path: &str) -> Result<Box<dyn Read>> {
        Err(IfixError::Json(
            "content_file is not available for in-memory test builds".into(),
        ))
    }
}

/// Parse-and-open helper over a byte vector (tests).
pub fn open_memory(bytes: Vec<u8>, limits: Limits) -> Result<IndexFile<MemStorage>> {
    IndexFile::open(MemStorage::new(bytes), limits)
}
