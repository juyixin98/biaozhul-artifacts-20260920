//! JSON control entry: a stateless request/response layer over the bitmap
//! library. One JSON object in, one JSON object out — usable from stdin,
//! a file, or embedded in any transport.
//!
//! # Protocol
//!
//! Every request is a JSON object:
//!
//! ```json
//! {
//!   "op": "build | union | intersect | difference | insert | remove |
//!          contains | decode | stats",
//!   "values": [1, 2, 3],                 // build
//!   "set": "<base64 RBM1>",              // unary ops
//!   "other": "<base64 RBM1>",            // binary ops
//!   "value": 42,                         // insert/remove/contains
//!   "limits": {                          // optional, all fields optional
//!     "max_containers": 65536,
//!     "max_values": 268435456,
//!     "max_output": 1048576
//!   }
//! }
//! ```
//!
//! Every response is `{"ok": true, "result": {...}}` or
//! `{"ok": false, "error": "<message>"}`. Encoded sets travel as base64.
//! `decode` refuses to materialize more than `max_output` values.

use crate::base64;
use crate::bitmap::{Bitmap, Limits};
use crate::error::{RbError, RbResult};
use crate::json::{self, Json};

/// Default cap on how many values `decode` will list.
pub const DEFAULT_MAX_OUTPUT: u64 = 1 << 20;

struct RequestLimits {
    decode: Limits,
    max_output: usize,
}

fn parse_limits(req: &Json) -> RbResult<RequestLimits> {
    let mut limits = Limits::default();
    let mut max_output = DEFAULT_MAX_OUTPUT;
    if let Some(l) = req.get("limits") {
        if let Some(v) = l.get("max_containers") {
            limits.max_containers = v
                .as_u64()
                .ok_or_else(|| RbError::InvalidInput("max_containers must be a u64".into()))?;
        }
        if let Some(v) = l.get("max_values") {
            limits.max_values = v
                .as_u64()
                .ok_or_else(|| RbError::InvalidInput("max_values must be a u64".into()))?;
        }
        if let Some(v) = l.get("max_output") {
            max_output = v
                .as_u64()
                .ok_or_else(|| RbError::InvalidInput("max_output must be a u64".into()))?;
        }
    }
    Ok(RequestLimits {
        decode: limits,
        max_output: max_output as usize,
    })
}

fn decode_set_field(req: &Json, field: &str, limits: Limits) -> RbResult<Bitmap> {
    let b64 = req
        .get(field)
        .and_then(Json::as_str)
        .ok_or_else(|| RbError::InvalidInput(format!("missing string field \"{field}\"")))?;
    let bytes = base64::decode(b64)?;
    Bitmap::decode(&bytes, limits)
}

fn stats_of(bm: &Bitmap) -> Json {
    Json::Obj(vec![
        ("cardinality".into(), Json::Num(bm.len() as f64)),
        ("containers".into(), Json::Num(bm.container_count() as f64)),
        (
            "array_containers".into(),
            Json::Num(bm.array_container_count() as f64),
        ),
        (
            "bitmap_containers".into(),
            Json::Num(bm.bitmap_container_count() as f64),
        ),
        ("encoded_bytes".into(), Json::Num(bm.encode().len() as f64)),
    ])
}

fn encoded_result(bm: &Bitmap) -> Json {
    Json::Obj(vec![
        ("set".into(), Json::Str(base64::encode(&bm.encode()))),
        ("stats".into(), stats_of(bm)),
    ])
}

/// Execute one JSON request, returning the JSON response.
pub fn handle(request_text: &str) -> String {
    let response = match handle_inner(request_text) {
        Ok(result) => Json::Obj(vec![
            ("ok".into(), Json::Bool(true)),
            ("result".into(), result),
        ]),
        Err(e) => Json::Obj(vec![
            ("ok".into(), Json::Bool(false)),
            ("error".into(), Json::Str(e.to_string())),
        ]),
    };
    json::stringify(&response)
}

fn handle_inner(request_text: &str) -> RbResult<Json> {
    let req = json::parse(request_text)?;
    let op = req
        .get("op")
        .and_then(Json::as_str)
        .ok_or_else(|| RbError::InvalidInput("missing string field \"op\"".into()))?;
    let limits = parse_limits(&req)?;

    match op {
        "build" => {
            let values = req
                .get("values")
                .and_then(Json::as_array)
                .ok_or_else(|| RbError::InvalidInput("missing array field \"values\"".into()))?;
            if values.len() as u64 > limits.decode.max_values {
                return Err(RbError::LengthExceeded {
                    what: "input value list",
                    declared: values.len() as u64,
                    limit: limits.decode.max_values,
                });
            }
            let mut bm = Bitmap::new();
            for v in values {
                let n = v
                    .as_u32()
                    .ok_or_else(|| RbError::InvalidInput("values must be u32 integers".into()))?;
                bm.insert(n);
            }
            Ok(encoded_result(&bm))
        }
        "union" | "intersect" | "difference" => {
            let a = decode_set_field(&req, "set", limits.decode)?;
            let b = decode_set_field(&req, "other", limits.decode)?;
            let out = match op {
                "union" => a.union(&b),
                "intersect" => a.intersect(&b),
                _ => a.difference(&b),
            };
            Ok(encoded_result(&out))
        }
        "insert" | "remove" => {
            let mut bm = decode_set_field(&req, "set", limits.decode)?;
            let value = req
                .get("value")
                .and_then(Json::as_u32)
                .ok_or_else(|| RbError::InvalidInput("missing u32 field \"value\"".into()))?;
            let changed = if op == "insert" {
                bm.insert(value)
            } else {
                bm.remove(value)
            };
            Ok(Json::Obj(vec![
                ("changed".into(), Json::Bool(changed)),
                ("set".into(), Json::Str(base64::encode(&bm.encode()))),
                ("stats".into(), stats_of(&bm)),
            ]))
        }
        "contains" => {
            let bm = decode_set_field(&req, "set", limits.decode)?;
            let value = req
                .get("value")
                .and_then(Json::as_u32)
                .ok_or_else(|| RbError::InvalidInput("missing u32 field \"value\"".into()))?;
            Ok(Json::Obj(vec![(
                "present".into(),
                Json::Bool(bm.contains(value)),
            )]))
        }
        "decode" => {
            let bm = decode_set_field(&req, "set", limits.decode)?;
            let values = bm.to_vec(limits.max_output)?;
            Ok(Json::Obj(vec![
                (
                    "values".into(),
                    Json::Arr(values.iter().map(|&v| Json::Num(v as f64)).collect()),
                ),
                ("stats".into(), stats_of(&bm)),
            ]))
        }
        "stats" => {
            let bm = decode_set_field(&req, "set", limits.decode)?;
            Ok(stats_of(&bm))
        }
        other => Err(RbError::InvalidInput(format!("unknown op \"{other}\""))),
    }
}
