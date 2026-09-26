//! JSON control entry: parses a JSON request, dispatches to the codec, and
//! returns a JSON response. Binary payloads are base64 strings.
//!
//! Requests (the `op` field selects the operation):
//!
//! ```json
//! {"op": "encode", "values": [1, 2, 3], "block_size": 128}
//! {"op": "decode", "data": "<base64>", "max_output": 1000}
//! {"op": "decode", "data": "<base64>", "block_index": 2}
//! {"op": "inspect", "data": "<base64>"}
//! ```
//!
//! Responses carry `"ok": true` on success or `"ok": false` plus an `"error"`
//! message on failure.

use serde::Deserialize;
use serde_json::json;

use crate::decode::{self, Limits};
use crate::encode::{self, EncodeOptions};
use crate::error::Error;
use crate::format::{DEFAULT_BLOCK_SIZE, MAX_BLOCK_VALUES};
use crate::base64;

/// Absolute ceiling for a caller-requested output limit.
const HARD_MAX_OUTPUT: u64 = 1 << 26;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
enum Request {
    Encode {
        values: Vec<i64>,
        block_size: Option<u32>,
    },
    Decode {
        data: String,
        max_output: Option<u64>,
        block_index: Option<u32>,
    },
    Inspect {
        data: String,
    },
}

fn op_encode(values: &[i64], block_size: Option<u32>) -> Result<serde_json::Value, Error> {
    let limits = Limits::default();
    if values.len() as u64 > limits.max_total_values {
        return Err(Error::LimitExceeded("encode input values"));
    }
    let opts = EncodeOptions {
        block_size: block_size.unwrap_or(DEFAULT_BLOCK_SIZE),
    };
    if opts.block_size == 0 || opts.block_size > MAX_BLOCK_VALUES {
        return Err(Error::InvalidData(format!(
            "block_size must be in 1..={MAX_BLOCK_VALUES}"
        )));
    }
    let blob = encode::encode(values, &opts)?;
    let info = decode::inspect(&blob, &limits)?;
    Ok(json!({
        "ok": true,
        "data": base64::encode(&blob),
        "stats": {
            "input_values": values.len(),
            "blocks": info.block_count,
            "bytes": blob.len(),
        }
    }))
}

fn op_decode(
    data: &str,
    max_output: Option<u64>,
    block_index: Option<u32>,
) -> Result<serde_json::Value, Error> {
    let blob = base64::decode(data)?;
    let mut limits = Limits::default();
    if let Some(m) = max_output {
        limits.max_output_values = m.min(HARD_MAX_OUTPUT);
    }
    let values = match block_index {
        Some(i) => decode::decode_block(&blob, i, &limits)?,
        None => decode::decode_all(&blob, &limits)?,
    };
    Ok(json!({
        "ok": true,
        "values": values,
        "stats": { "output_values": values.len() }
    }))
}

fn op_inspect(data: &str) -> Result<serde_json::Value, Error> {
    let blob = base64::decode(data)?;
    let info = decode::inspect(&blob, &Limits::default())?;
    Ok(json!({
        "ok": true,
        "header": {
            "version": info.version,
            "block_count": info.block_count,
            "total_values": info.total_values,
            "index_offset": info.index_offset,
        },
        "index": info.index.iter().map(|e| json!({
            "offset": e.offset,
            "first_value": e.first_value,
            "count": e.count,
        })).collect::<Vec<_>>(),
        "blocks": info.blocks.iter().map(|b| json!({
            "count": b.count,
            "base": b.base,
            "bit_width": b.bit_width,
            "escape_count": b.escape_count,
            "total_len": b.total_len,
        })).collect::<Vec<_>>(),
    }))
}

fn dispatch(input: &str) -> Result<serde_json::Value, Error> {
    let req: Request = serde_json::from_str(input).map_err(|e| Error::Json(e.to_string()))?;
    match req {
        Request::Encode { values, block_size } => op_encode(&values, block_size),
        Request::Decode {
            data,
            max_output,
            block_index,
        } => op_decode(&data, max_output, block_index),
        Request::Inspect { data } => op_inspect(&data),
    }
}

/// Handle one JSON request string. Returns the response JSON and whether the
/// request succeeded.
pub fn handle_request(input: &str) -> (String, bool) {
    match dispatch(input) {
        Ok(v) => (v.to_string(), true),
        Err(e) => (json!({ "ok": false, "error": e.to_string() }).to_string(), false),
    }
}
