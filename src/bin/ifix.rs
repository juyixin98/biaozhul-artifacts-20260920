//! `ifix` — command-line / JSON control entry point for the IFIX library.
//!
//! Two invocation styles share one implementation:
//!
//! 1. JSON control protocol (the primary interface):
//!    `ifix run --request request.json`
//!    The request is one JSON object `{"op": ..., ...}`; the response is one
//!    JSON object on stdout (`{"ok": true, "result": ...}` or
//!    `{"ok": false, "error": {"kind": ..., "message": ...}}`).
//! 2. Convenience subcommands (`build`, `validate`, `lookup`, `list`, `read`,
//!    `crosscheck`) which build the same request from flags.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use ifix::json::{self, Value};
use ifix::reader::Reader;
use ifix::validate;
use ifix::writer::build_from_json;
use ifix::{Error, Limits, NodeType, Result, WriteOptions};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = dispatch(&args);
    match result {
        Ok(response) => {
            println!("{}", json::to_string_pretty(&response));
            ExitCode::SUCCESS
        }
        Err(err) => {
            let response = error_response(&err);
            println!("{}", json::to_string_pretty(&response));
            eprintln!("ifix: {err}");
            ExitCode::from(1)
        }
    }
}

fn dispatch(args: &[String]) -> Result<Value> {
    if args.is_empty() {
        return Err(Error::Json(usage()));
    }
    match args[0].as_str() {
        "run" => {
            let mut request_path: Option<&str> = None;
            let mut i = 1;
            while i < args.len() {
                match args[i].as_str() {
                    "--request" => {
                        request_path = Some(
                            args.get(i + 1)
                                .ok_or_else(|| Error::Json("--request requires a path".into()))?,
                        );
                        i += 2;
                    }
                    other => return Err(Error::Json(format!("unknown flag {other}"))),
                }
            }
            let path = request_path
                .ok_or_else(|| Error::Json("usage: ifix run --request <file>".into()))?;
            let text = fs::read_to_string(path)
                .map_err(|e| Error::Json(format!("cannot read request {path}: {e}")))?;
            let request = json::parse(&text)
                .map_err(|e| Error::Json(format!("request is not valid JSON: {e}")))?;
            execute(&request, None)
        }
        "build" | "validate" | "lookup" | "list" | "read" | "crosscheck" => {
            let request = flags_to_request(&args[0], &args[1..])?;
            execute(&request, None)
        }
        "--help" | "-h" | "help" => {
            println!("{USAGE}");
            Ok(Value::Object(vec![("ok".into(), Value::Bool(true))]))
        }
        other => Err(Error::Json(format!(
            "unknown subcommand {other}\n\n{USAGE}"
        ))),
    }
}

const USAGE: &str = concat!(
    "ifix — immutable file index tool\n\n",
    "JSON protocol:\n",
    "  ifix run --request request.json\n\n",
    "Subcommands:\n",
    "  ifix build --request tree.json --output archive.ifix\n",
    "  ifix validate --file archive.ifix [--backend file|mmap]\n",
    "  ifix lookup   --file archive.ifix --path /dir/file [--no-follow] [--backend]\n",
    "  ifix list     --file archive.ifix --path / [--backend]\n",
    "  ifix read     --file archive.ifix --path /dir/file [--offset N] [--len N]\n",
    "                [--output-file blob.bin] [--backend]\n",
    "  ifix crosscheck --file archive.ifix\n",
    "\nSee examples/ for request samples and docs/FORMAT.md for the format.\n",
);

fn usage() -> String {
    USAGE.into()
}

fn flags_to_request(op: &str, flags: &[String]) -> Result<Value> {
    let mut entries = vec![("op".to_string(), Value::Str(op.to_string()))];
    let mut i = 0;
    while i < flags.len() {
        let flag = flags[i].as_str();
        let takes = !matches!(flag, "--no-follow");
        let value = if takes {
            Some(
                flags
                    .get(i + 1)
                    .ok_or_else(|| Error::Json(format!("flag {flag} requires a value")))?
                    .clone(),
            )
        } else {
            None
        };
        let key = flag.trim_start_matches('-');
        match key {
            "no-follow" => entries.push(("follow".into(), Value::Bool(false))),
            "offset" | "len" => {
                let v = value.unwrap();
                let n: i64 = v
                    .parse()
                    .map_err(|_| Error::Json(format!("{flag} must be an integer")))?;
                entries.push((key.into(), Value::Int(n)));
            }
            _ => entries.push((key.into(), Value::Str(value.unwrap()))),
        }
        i += if takes { 2 } else { 1 };
    }
    Ok(Value::Object(entries))
}

// ------------------------------------------------------------- request exec

fn execute(request: &Value, base_dir: Option<&Path>) -> Result<Value> {
    let op = request
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::Json("request missing string field \"op\"".into()))?;
    match op {
        "build" => op_build(request, base_dir),
        "validate" => op_validate(request),
        "lookup" => op_lookup(request),
        "list" => op_list(request),
        "read" => op_read(request),
        "crosscheck" => op_crosscheck(request),
        other => Err(Error::Json(format!("unknown op {other:?}"))),
    }
}

fn limits_of(request: &Value) -> Limits {
    // A small optional "limits" object may tighten the defaults.
    let mut l = Limits::default();
    if let Some(obj) = request.get("limits").and_then(Value::as_object) {
        for (k, v) in obj {
            let n = v.as_i64().unwrap_or(0).max(0) as u64;
            match k.as_str() {
                "max_file_len" => l.max_file_len = n,
                "max_nodes" => l.max_nodes = n,
                "max_blocks" => l.max_blocks = n,
                "max_output" => l.max_output = n,
                "max_name_len" => l.max_name_len = n,
                "max_listing" => l.max_listing = n,
                _ => {}
            }
        }
    }
    l
}

fn open_reader(file: &str, backend: &str, limits: Limits) -> Result<Reader> {
    match backend {
        "file" | "stream" => Reader::open_file(file, limits),
        "mmap" => Reader::open_mmap(file, limits),
        other => Err(Error::Json(format!(
            "unknown backend {other:?} (file|mmap)"
        ))),
    }
}

fn op_build(request: &Value, base_dir: Option<&Path>) -> Result<Value> {
    // Allow request to inline a literal tree object or reference a file.
    let tree = match request.get("tree") {
        Some(t) => t.clone(),
        None => match request.get("tree_file").and_then(Value::as_str) {
            Some(p) => {
                let path = resolve(base_dir, p);
                let text = fs::read_to_string(&path)
                    .map_err(|e| Error::Json(format!("cannot read tree file {p}: {e}")))?;
                json::parse(&text)
                    .map_err(|e| Error::Json(format!("tree file is not valid JSON: {e}")))?
            }
            None => return Err(Error::Json("build needs \"tree\" or \"tree_file\"".into())),
        },
    };
    // Inline content from referenced host files before encoding.
    let tree = inline_content_files(tree, base_dir)?;

    let full = Value::Object(vec![
        ("tree".to_string(), tree),
        (
            "limits".to_string(),
            request.get("limits").cloned().unwrap_or(Value::Null),
        ),
    ]);
    let image = build_from_json(
        &full,
        WriteOptions {
            limits: limits_of(request),
        },
    )?;

    let output = request
        .get("output")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::Json("build needs string field \"output\"".into()))?;
    let out_path = resolve(base_dir, output);
    let mut f = fs::File::create(&out_path)?;
    f.write_all(&image)?;
    f.sync_all()?;

    // Immediately re-open both backends and validate what was just written.
    let report_stream = validate::validate(&Reader::open_file(&out_path, Limits::default())?)?;
    let report_mmap = validate::validate(&Reader::open_mmap(&out_path, Limits::default())?)?;
    if report_stream.nodes != report_mmap.nodes || report_stream.blocks != report_mmap.blocks {
        return Err(Error::CorruptIndex {
            detail: "post-build stream/mmap reports disagree",
        });
    }

    Ok(result_obj(vec![
        ("output".into(), Value::Str(out_path.display().to_string())),
        ("bytes".into(), Value::Int(image.len() as i64)),
        ("nodes".into(), Value::Int(report_stream.nodes as i64)),
        ("blocks".into(), Value::Int(report_stream.blocks as i64)),
        ("dirs".into(), Value::Int(report_stream.dirs as i64)),
        (
            "file_bytes".into(),
            Value::Int(report_stream.file_bytes as i64),
        ),
    ]))
}

/// Replace each node's `content_file` (host path) with `content_base64`.
fn inline_content_files(tree: Value, base_dir: Option<&Path>) -> Result<Value> {
    match tree {
        Value::Object(mut entries) => {
            let has_inline = entries.iter().any(|(k, _)| k == "content_base64");
            let mut file_ref: Option<String> = None;
            for (k, v) in &entries {
                if k == "content_file" {
                    file_ref = v.as_str().map(str::to_string);
                }
            }
            if let Some(p) = file_ref {
                if has_inline {
                    return Err(Error::Json(
                        "node cannot set both content_base64 and content_file".into(),
                    ));
                }
                let bytes = fs::read(resolve(base_dir, &p))
                    .map_err(|e| Error::Json(format!("cannot read content_file {p}: {e}")))?;
                entries.push((
                    "content_base64".into(),
                    Value::Str(ifix::base64::encode(&bytes)),
                ));
            }
            let mut done = Vec::with_capacity(entries.len());
            for (k, v) in entries {
                done.push((k, inline_content_files(v, base_dir)?));
            }
            Ok(Value::Object(done))
        }
        Value::Array(items) => Ok(Value::Array(
            items
                .into_iter()
                .map(|v| inline_content_files(v, base_dir))
                .collect::<Result<Vec<_>>>()?,
        )),
        other => Ok(other),
    }
}

fn resolve(base_dir: Option<&Path>, p: &str) -> PathBuf {
    let path = PathBuf::from(p);
    match base_dir {
        Some(dir) if !path.is_absolute() => dir.join(path),
        _ => path,
    }
}

fn op_validate(request: &Value) -> Result<Value> {
    let file = required_str(request, "file")?;
    let backend = request
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("file");
    let reader = open_reader(file, backend, limits_of(request))?;
    let report = validate::validate(&reader)?;
    Ok(result_obj(vec![
        ("file".into(), Value::Str(file.into())),
        ("backend".into(), Value::Str(backend.into())),
        ("nodes".into(), Value::Int(report.nodes as i64)),
        ("blocks".into(), Value::Int(report.blocks as i64)),
        ("dirs".into(), Value::Int(report.dirs as i64)),
        ("file_bytes".into(), Value::Int(report.file_bytes as i64)),
    ]))
}

fn op_lookup(request: &Value) -> Result<Value> {
    let (reader, path, follow) = query_params(request)?;
    let node = reader.lookup(&path, follow)?;
    let mut result = node_json(&reader, &node)?;
    if node.kind == NodeType::Symlink {
        let target = reader.symlink_target(&node)?;
        result.push(("target".into(), Value::Str(target)));
    }
    Ok(result_obj(result))
}

fn op_list(request: &Value) -> Result<Value> {
    let (reader, path, _follow) = query_params(request)?;
    let entries = reader.list(&path)?;
    let items: Vec<Value> = entries
        .iter()
        .map(|e| {
            Value::Object(vec![
                ("name".into(), Value::Str(e.name.clone())),
                ("node_id".into(), Value::Int(e.node_id as i64)),
                ("type".into(), Value::Str(type_name(e.kind).into())),
                ("size".into(), Value::Int(e.size as i64)),
            ])
        })
        .collect();
    Ok(result_obj(vec![
        ("path".into(), Value::Str(path)),
        ("count".into(), Value::Int(items.len() as i64)),
        ("entries".into(), Value::Array(items)),
    ]))
}

fn op_read(request: &Value) -> Result<Value> {
    let (reader, path, follow) = query_params(request)?;
    let node = reader.lookup(&path, follow)?;
    if node.kind != NodeType::File {
        return Err(Error::BadNodeRecord {
            index: node.id,
            detail: "read target is not a regular file",
        });
    }
    let offset = request
        .get("offset")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .max(0) as u64;
    let len = request
        .get("len")
        .and_then(Value::as_i64)
        .map(|v| v.max(0) as u64);
    let bytes = {
        let mut buf = Vec::new();
        let n = reader.copy_file(&node, &mut buf, offset, len)?;
        debug_assert_eq!(n as usize, buf.len());
        buf
    };
    if let Some(out) = request.get("output_file").and_then(Value::as_str) {
        fs::write(out, &bytes)?;
    }
    let max_inline = request
        .get("max_inline_base64")
        .and_then(Value::as_i64)
        .unwrap_or(4096)
        .max(0) as usize;
    let mut fields = vec![
        ("path".into(), Value::Str(path)),
        ("offset".into(), Value::Int(offset as i64)),
        ("bytes".into(), Value::Int(bytes.len() as i64)),
    ];
    if bytes.len() <= max_inline {
        fields.push((
            "content_base64".into(),
            Value::Str(ifix::base64::encode(&bytes)),
        ));
    } else {
        fields.push((
            "content_base64".into(),
            Value::Str(format!("<{} bytes omitted; set output_file>", bytes.len())),
        ));
    }
    Ok(result_obj(fields))
}

/// Run the same operations through the streaming reader and the mmap reader
/// and assert byte-identical results.
fn op_crosscheck(request: &Value) -> Result<Value> {
    let file = required_str(request, "file")?;
    let limits = limits_of(request);
    let rs = Reader::open_file(file, limits)?;
    let rm = Reader::open_mmap(file, limits)?;

    let rep_s = validate::validate(&rs)?;
    let rep_m = validate::validate(&rm)?;
    let mut checks: Vec<Value> = Vec::new();
    let mut equal = rep_s.nodes == rep_m.nodes
        && rep_s.blocks == rep_m.blocks
        && rep_s.dirs == rep_m.dirs
        && rep_s.file_bytes == rep_m.file_bytes;
    checks.push(check_obj("validate", true, equal, ""));

    // Compare full recursive listings and every file payload.
    let entries_s = rs.list("/")?;
    let entries_m = rm.list("/")?;
    let same_listing = entries_s.len() == entries_m.len()
        && entries_s.iter().zip(entries_m.iter()).all(|(a, b)| {
            a.name == b.name && a.node_id == b.node_id && a.kind == b.kind && a.size == b.size
        });
    equal &= same_listing;
    checks.push(check_obj("list:/", true, same_listing, ""));

    // Recursively compare every path lookup and file bytes.
    let mut stack: Vec<String> = vec![String::new()];
    let mut paths: Vec<String> = Vec::new();
    while let Some(pre) = stack.pop() {
        let l = rs.list(&pre)?;
        for e in l {
            let full = if pre.is_empty() || pre == "/" {
                format!("/{}", e.name)
            } else {
                format!("{}/{}", pre, e.name)
            };
            paths.push(full.clone());
            if e.kind == NodeType::Dir {
                stack.push(full);
            }
        }
    }
    let mut bytes_checked = 0u64;
    for p in &paths {
        let ns = rs.lookup(p, false)?;
        let nm = rm.lookup(p, false)?;
        let same_meta = ns.kind == nm.kind
            && ns.data == nm.data
            && ns.name == nm.name
            && ns.mode == nm.mode
            && ns.child_count == nm.child_count
            && ns.first_block == nm.first_block;
        if !same_meta {
            equal = false;
            checks.push(check_obj(
                &format!("lookup:{p}"),
                false,
                false,
                "metadata differs",
            ));
            continue;
        }
        if ns.kind == NodeType::File {
            let a = rs.read_file(&ns)?;
            let b = rm.read_file(&nm)?;
            bytes_checked += a.len() as u64;
            if a != b {
                equal = false;
                checks.push(check_obj(
                    &format!("read:{p}"),
                    false,
                    false,
                    "payload differs",
                ));
            }
        }
    }
    checks.push(check_obj(
        "all-paths",
        true,
        true,
        &format!(
            "{} paths, {bytes_checked} payload bytes compared",
            paths.len()
        ),
    ));

    Ok(result_obj(vec![
        ("file".into(), Value::Str(file.into())),
        ("paths".into(), Value::Int(paths.len() as i64)),
        ("payload_bytes".into(), Value::Int(bytes_checked as i64)),
        ("equal".into(), Value::Bool(equal)),
        ("checks".into(), Value::Array(checks)),
    ]))
}

// --------------------------------------------------------------- small helpers

fn query_params(request: &Value) -> Result<(Reader, String, bool)> {
    let file = required_str(request, "file")?;
    let path = required_str(request, "path")?.to_owned();
    let backend = request
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("file");
    let follow = request
        .get("follow")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let reader = open_reader(file, backend, limits_of(request))?;
    Ok((reader, path, follow))
}

fn required_str<'a>(request: &'a Value, key: &str) -> Result<&'a str> {
    request
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| Error::Json(format!("missing string field {key:?}")))
}

fn type_name(k: NodeType) -> &'static str {
    match k {
        NodeType::File => "file",
        NodeType::Dir => "dir",
        NodeType::Symlink => "symlink",
    }
}

fn node_json(reader: &Reader, node: &ifix::reader::NodeInfo) -> Result<Vec<(String, Value)>> {
    let name = reader.node_name(node)?;
    Ok(vec![
        ("node_id".into(), Value::Int(node.id as i64)),
        ("name".into(), Value::Str(name)),
        ("type".into(), Value::Str(type_name(node.kind).into())),
        ("size".into(), Value::Int(node.data.1 as i64)),
        ("mode".into(), Value::Int(node.mode as i64)),
        ("mtime".into(), Value::Int(node.mtime as i64)),
        ("child_count".into(), Value::Int(node.child_count as i64)),
    ])
}

fn result_obj(fields: Vec<(String, Value)>) -> Value {
    let mut v = vec![("ok".to_string(), Value::Bool(true))];
    v.push(("result".to_string(), Value::Object(fields)));
    Value::Object(v)
}

fn check_obj(name: &str, passed: bool, equal: bool, detail: &str) -> Value {
    Value::Object(vec![
        ("name".into(), Value::Str(name.into())),
        ("expected".into(), Value::Bool(passed)),
        ("equal".into(), Value::Bool(equal)),
        ("detail".into(), Value::Str(detail.into())),
    ])
}

fn error_response(err: &Error) -> Value {
    Value::Object(vec![
        ("ok".to_string(), Value::Bool(false)),
        (
            "error".to_string(),
            Value::Object(vec![
                ("kind".to_string(), Value::Str(error_kind(err).to_string())),
                ("message".to_string(), Value::Str(err.to_string())),
            ]),
        ),
    ])
}

fn error_kind(err: &Error) -> &'static str {
    match err {
        Error::Io(_) => "io",
        Error::Json(_) => "bad_request",
        Error::NotFound(_) => "not_found",
        Error::NotADirectory(_) => "not_a_directory",
        Error::LimitExceeded { .. } => "limit_exceeded",
        Error::LinkCycleOrDepth(_) => "link_cycle",
        Error::TruncatedHeader | Error::SectionTruncated { .. } | Error::ShortRead { .. } => {
            "truncated"
        }
        _ => "corrupt",
    }
}
