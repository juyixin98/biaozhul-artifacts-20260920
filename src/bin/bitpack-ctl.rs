//! JSON control entry for the bitpack codec.
//!
//! Reads one JSON request from a file argument or stdin, writes one JSON
//! response to stdout. Exit code 0 on success, 1 on any error (the error
//! is still reported as JSON).
//!
//! Requests:
//!   {"op":"encode","values":[...],"block_size":128}
//!   {"op":"decode","data":"<base64>","limits":{...}}
//!   {"op":"decode_block","data":"<base64>","block":0,"limits":{...}}
//!   {"op":"inspect","data":"<base64>","limits":{...}}

use std::io::Read;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use bitpack::limits::Limits;
use bitpack::{decode_block_at, decode_stream, encode_stream, inspect, Error};
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
struct Request {
    op: String,
    #[serde(default)]
    values: Option<Vec<i64>>,
    #[serde(default)]
    block_size: Option<u32>,
    #[serde(default)]
    block: Option<u32>,
    #[serde(default)]
    data: Option<String>,
    #[serde(default)]
    limits: Option<LimitsRequest>,
}

#[derive(Deserialize)]
struct LimitsRequest {
    max_input_bytes: Option<usize>,
    max_total_values: Option<u64>,
    max_block_size: Option<u32>,
    max_output_values: Option<u64>,
}

impl LimitsRequest {
    fn apply(self, base: Limits) -> Limits {
        Limits {
            max_input_bytes: self.max_input_bytes.unwrap_or(base.max_input_bytes),
            max_total_values: self.max_total_values.unwrap_or(base.max_total_values),
            max_block_size: self.max_block_size.unwrap_or(base.max_block_size),
            max_output_values: self.max_output_values.unwrap_or(base.max_output_values),
        }
    }
}

const DEFAULT_BLOCK_SIZE: u32 = 128;

fn main() {
    let code = match run() {
        Ok(response) => {
            println!("{response}");
            0
        }
        Err(e) => {
            println!("{}", json!({"ok": false, "error": e.to_string()}));
            1
        }
    };
    std::process::exit(code);
}

fn run() -> Result<Value, Error> {
    let input = read_input()?;
    let request: Request = serde_json::from_str(&input)
        .map_err(|e| Error::BadRequest(format!("invalid JSON: {e}")))?;
    dispatch(request).map(|v| {
        let mut obj = v;
        obj.as_object_mut()
            .expect("responses are objects")
            .insert("ok".to_string(), Value::Bool(true));
        obj
    })
}

fn read_input() -> Result<String, Error> {
    let mut args = std::env::args().skip(1);
    match args.next() {
        Some(path) if path == "-h" || path == "--help" => {
            println!("usage: bitpack-ctl [request.json]  (reads stdin when no file is given)");
            std::process::exit(0);
        }
        Some(path) => Ok(std::fs::read_to_string(&path)
            .map_err(|e| Error::BadRequest(format!("cannot read {path}: {e}")))?),
        None => {
            let mut buf = String::new();
            std::io::stdin().read_to_string(&mut buf)?;
            Ok(buf)
        }
    }
}

fn dispatch(request: Request) -> Result<Value, Error> {
    match request.op.as_str() {
        "encode" => {
            let values = request
                .values
                .as_deref()
                .ok_or_else(|| Error::BadRequest("encode requires \"values\"".into()))?;
            let block_size = request.block_size.unwrap_or(DEFAULT_BLOCK_SIZE);
            let data = encode_stream(values, block_size)?;
            let info = inspect(&data, &limits_of(&request)?)?;
            Ok(json!({
                "data": B64.encode(&data),
                "stats": {
                    "total_values": info.header.total_values,
                    "block_count": info.header.block_count,
                    "block_size": info.header.block_size,
                    "byte_len": data.len(),
                }
            }))
        }
        "decode" => {
            let (data, limits) = decode_payload(&request)?;
            let values = decode_stream(&data, &limits)?;
            Ok(json!({"values": values}))
        }
        "decode_block" => {
            let (data, limits) = decode_payload(&request)?;
            let block = request
                .block
                .ok_or_else(|| Error::BadRequest("decode_block requires \"block\"".into()))?;
            let values = decode_block_at(&data, block, &limits)?;
            Ok(json!({"block": block, "values": values}))
        }
        "inspect" => {
            let (data, limits) = decode_payload(&request)?;
            let info = inspect(&data, &limits)?;
            Ok(json!({
                "header": {
                    "block_size": info.header.block_size,
                    "block_count": info.header.block_count,
                    "total_values": info.header.total_values,
                    "index_offset": info.header.index_offset,
                },
                "index": info.index.iter().map(|e| json!({
                    "block_offset": e.block_offset,
                    "first_value_index": e.first_value_index,
                })).collect::<Vec<_>>(),
            }))
        }
        other => Err(Error::BadRequest(format!("unknown op \"{other}\""))),
    }
}

fn limits_of(request: &Request) -> Result<Limits, Error> {
    Ok(match &request.limits {
        Some(l) => LimitsRequest {
            max_input_bytes: l.max_input_bytes,
            max_total_values: l.max_total_values,
            max_block_size: l.max_block_size,
            max_output_values: l.max_output_values,
        }
        .apply(Limits::default()),
        None => Limits::default(),
    })
}

fn decode_payload(request: &Request) -> Result<(Vec<u8>, Limits), Error> {
    let b64 = request
        .data
        .as_deref()
        .ok_or_else(|| Error::BadRequest(format!("op \"{}\" requires \"data\"", request.op)))?;
    let data = B64.decode(b64).map_err(|e| Error::BadBase64(e.to_string()))?;
    Ok((data, limits_of(request)?))
}
