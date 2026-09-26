//! JSON control plane: a single entry point that executes codec operations
//! described by a JSON request document.
//!
//! Request schema (see `README.md` §"JSON 控制入口" and `examples/`):
//!
//! ```json
//! {
//!   "op": "encode" | "decode" | "merge" | "inspect",
//!   "input":  "path-or--",            // encode/decode/inspect ("-" = stdin)
//!   "inputs": ["p1", "p2"],           // merge
//!   "output": "path-or--",            // encode/decode/merge ("-" = stdout)
//!   "segment_rows": 100000,           // encode/merge; 0/absent = one segment
//!   "limits": { "max_rows": 1000, ... }   // optional per-field overrides
//! }
//! ```
//!
//! Encode input / decode output row format: a JSON array of `string|null`.
//!
//! The response is a JSON object: `{"ok": true, ...stats}` on success or
//! `{"ok": false, "error": "..."}` on failure. When `output` is `"-"` the
//! data goes to stdout and the response is meant for stderr (the CLI handles
//! that split).

use crate::decoder;
use crate::encoder::Encoder;
use crate::error::{Error, Limits, Result};
use crate::json::{self, Json};
use crate::merge::Merger;
use std::io::{BufWriter, Read, Write};

/// Hard cap on control-plane input files (JSON documents and binary streams
/// read into memory). The codec itself streams; this bounds the control
/// plane's convenience I/O. 1 GiB.
pub const MAX_CONTROL_INPUT_BYTES: u64 = 1 << 30;

/// Execute one control request. `stdout_data` receives operation output when
/// the request's `output` is `"-"`. Returns the response object.
pub fn execute(req: &Json, stdout_data: &mut dyn Write) -> Result<Json> {
    let op = req
        .get("op")
        .and_then(Json::as_str)
        .ok_or_else(|| Error::invalid("request missing string field \"op\""))?;
    let limits = parse_limits(req.get("limits"))?;
    match op {
        "encode" => op_encode(req, &limits, stdout_data),
        "decode" => op_decode(req, &limits, stdout_data),
        "merge" => op_merge(req, &limits, stdout_data),
        "inspect" => op_inspect(req, &limits),
        other => Err(Error::invalid(format!("unknown op {other:?}"))),
    }
}

fn parse_limits(v: Option<&Json>) -> Result<Limits> {
    /// Accessor for one overridable field of [`Limits`].
    type LimitField = fn(&mut Limits) -> &mut u64;
    let mut limits = Limits::default();
    let Some(v) = v else { return Ok(limits) };
    let fields: [(&str, LimitField); 6] = [
        ("max_dict_bytes", |l| &mut l.max_dict_bytes),
        ("max_dict_entries", |l| &mut l.max_dict_entries),
        ("max_rows", |l| &mut l.max_rows),
        ("max_string_bytes", |l| &mut l.max_string_bytes),
        ("max_decoded_string_bytes", |l| {
            &mut l.max_decoded_string_bytes
        }),
        ("max_segments", |l| &mut l.max_segments),
    ];
    for (name, get) in fields {
        if let Some(raw) = v.get(name) {
            let val = raw.as_u64().ok_or_else(|| {
                Error::invalid(format!("limits.{name} must be a non-negative integer"))
            })?;
            *get(&mut limits) = val;
        }
    }
    Ok(limits)
}

fn segment_rows_of(req: &Json) -> Result<u64> {
    match req.get("segment_rows") {
        None | Some(Json::Null) => Ok(0),
        Some(v) => v
            .as_u64()
            .ok_or_else(|| Error::invalid("segment_rows must be a non-negative integer")),
    }
}

fn read_input(path: &str) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    let mut reader: Box<dyn Read> = if path == "-" {
        Box::new(std::io::stdin())
    } else {
        Box::new(
            std::fs::File::open(path)
                .map_err(|e| Error::Io(format!("cannot open input {path:?}: {e}")))?,
        )
    };
    // Read at most MAX_CONTROL_INPUT_BYTES + 1 to detect overflow.
    let mut limited = reader.by_ref().take(MAX_CONTROL_INPUT_BYTES + 1);
    limited.read_to_end(&mut buf).map_err(Error::io)?;
    if buf.len() as u64 > MAX_CONTROL_INPUT_BYTES {
        return Err(Error::limit(format!(
            "control-plane input exceeds {MAX_CONTROL_INPUT_BYTES} bytes"
        )));
    }
    Ok(buf)
}

fn open_output<'a>(
    path: &str,
    stdout_data: &'a mut dyn Write,
) -> Result<BufWriter<Box<dyn Write + 'a>>> {
    let w: Box<dyn Write + 'a> = if path == "-" {
        Box::new(stdout_data)
    } else {
        Box::new(
            std::fs::File::create(path)
                .map_err(|e| Error::Io(format!("cannot create output {path:?}: {e}")))?,
        )
    };
    Ok(BufWriter::new(w))
}

fn required_str<'a>(req: &'a Json, field: &str) -> Result<&'a str> {
    req.get(field)
        .and_then(Json::as_str)
        .ok_or_else(|| Error::invalid(format!("request missing string field {field:?}")))
}

fn ok_response(op: &str, extra: Vec<(String, Json)>) -> Json {
    let mut entries = vec![
        ("ok".to_string(), Json::Bool(true)),
        ("op".to_string(), Json::String(op.to_string())),
    ];
    entries.extend(extra);
    Json::Object(entries)
}

/// Parse the rows document: a JSON array of `string|null`.
fn parse_rows(bytes: &[u8]) -> Result<Vec<Option<String>>> {
    let doc = Json::parse(bytes)?;
    let arr = doc
        .as_array()
        .ok_or_else(|| Error::invalid("rows document must be a JSON array"))?;
    let mut rows = Vec::with_capacity(arr.len());
    for (i, item) in arr.iter().enumerate() {
        match item {
            Json::Null => rows.push(None),
            Json::String(s) => rows.push(Some(s.clone())),
            other => {
                return Err(Error::invalid(format!(
                    "row {i} must be a string or null, found {other:?}"
                )))
            }
        }
    }
    Ok(rows)
}

fn op_encode(req: &Json, limits: &Limits, stdout_data: &mut dyn Write) -> Result<Json> {
    let input = required_str(req, "input")?;
    let output = required_str(req, "output")?;
    let segment_rows = segment_rows_of(req)?;

    let rows = parse_rows(&read_input(input)?)?;
    if rows.len() as u64 > limits.max_rows {
        return Err(Error::limit(format!(
            "{} rows exceed max_rows {}",
            rows.len(),
            limits.max_rows
        )));
    }

    let mut out = open_output(output, stdout_data)?;
    let mut enc = Encoder::new(&mut out, limits.clone(), segment_rows);
    for row in &rows {
        enc.push_row(row.as_deref())?;
    }
    let stats = enc.finish()?;
    out.flush().map_err(Error::io)?;

    Ok(ok_response(
        "encode",
        vec![
            ("rows".into(), Json::Int(stats.rows as i64)),
            ("segments".into(), Json::Int(stats.segments as i64)),
        ],
    ))
}

fn op_decode(req: &Json, limits: &Limits, stdout_data: &mut dyn Write) -> Result<Json> {
    let input = required_str(req, "input")?;
    let output = required_str(req, "output")?;

    let bytes = read_input(input)?;
    let mut out = open_output(output, stdout_data)?;

    out.write_all(b"[").map_err(Error::io)?;
    let mut first = true;
    let stats = decoder::decode_for_each(&bytes[..], limits, |row| {
        if !first {
            out.write_all(b",").map_err(Error::io)?;
        }
        first = false;
        match row {
            None => out.write_all(b"null").map_err(Error::io),
            Some(s) => json::write_json_string(&mut out, s).map_err(Error::io),
        }
    })?;
    out.write_all(b"]").map_err(Error::io)?;
    out.flush().map_err(Error::io)?;

    Ok(ok_response(
        "decode",
        vec![
            ("rows".into(), Json::Int(stats.rows as i64)),
            ("segments".into(), Json::Int(stats.segments as i64)),
        ],
    ))
}

fn op_merge(req: &Json, limits: &Limits, stdout_data: &mut dyn Write) -> Result<Json> {
    let inputs = req
        .get("inputs")
        .and_then(Json::as_array)
        .ok_or_else(|| Error::invalid("merge request missing array field \"inputs\""))?;
    let output = required_str(req, "output")?;
    let segment_rows = segment_rows_of(req)?;

    let mut out = open_output(output, stdout_data)?;
    let mut merger = Merger::new(limits.clone(), segment_rows);
    merger.begin(&mut out)?;
    for (i, p) in inputs.iter().enumerate() {
        let path = p
            .as_str()
            .ok_or_else(|| Error::invalid(format!("inputs[{i}] must be a string path")))?;
        let bytes = read_input(path)?;
        merger.add_stream(&mut &bytes[..], &mut out)?;
    }
    let stats = merger.finish(&mut out)?;
    out.flush().map_err(Error::io)?;

    Ok(ok_response(
        "merge",
        vec![
            ("rows".into(), Json::Int(stats.rows as i64)),
            (
                "input_streams".into(),
                Json::Int(stats.input_streams as i64),
            ),
            (
                "input_segments".into(),
                Json::Int(stats.input_segments as i64),
            ),
            (
                "output_segments".into(),
                Json::Int(stats.output_segments as i64),
            ),
            (
                "global_dict_entries".into(),
                Json::Int(stats.global_dict_entries as i64),
            ),
        ],
    ))
}

fn op_inspect(req: &Json, limits: &Limits) -> Result<Json> {
    let input = required_str(req, "input")?;
    let bytes = read_input(input)?;

    let mut rows: u64 = 0;
    let mut null_rows: u64 = 0;
    let mut string_bytes: u64 = 0;
    let mut distinct: std::collections::HashSet<String> = std::collections::HashSet::new();
    let stats = decoder::decode_for_each(&bytes[..], limits, |row| {
        rows += 1;
        match row {
            None => null_rows += 1,
            Some(s) => {
                string_bytes += s.len() as u64;
                // Bounded by max_rows: at most one HashSet entry per row.
                distinct.insert(s.to_string());
            }
        }
        Ok(())
    })?;

    Ok(ok_response(
        "inspect",
        vec![
            ("rows".into(), Json::Int(rows as i64)),
            ("segments".into(), Json::Int(stats.segments as i64)),
            ("null_rows".into(), Json::Int(null_rows as i64)),
            ("string_bytes".into(), Json::Int(string_bytes as i64)),
            ("distinct_values".into(), Json::Int(distinct.len() as i64)),
        ],
    ))
}
