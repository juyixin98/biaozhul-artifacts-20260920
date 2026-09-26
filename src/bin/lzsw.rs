//! lzsw — JSON control entry point.
//!
//! Reads one JSON request from stdin, writes one JSON response to stdout.
//!
//! Request:
//!   {"op":"encode","data":"<base64>","chunk_size":4096}
//!   {"op":"decode","data":"<base64>","max_output":1048576,"chunk_size":4096}
//!
//! - `data`        base64-encoded input bytes (required)
//! - `max_output`  decode output budget in bytes (optional, default 64 MiB)
//! - `chunk_size`  feed the codec in chunks of this size to exercise the
//!   streaming path (optional, default: single shot)
//!
//! Response (success):
//!   {"ok":true,"op":"encode","input_len":N,"output_len":M,"data":"<base64>"}
//! Response (failure):
//!   {"ok":false,"error":{"kind":"InvalidDistance","message":"..."}}
//!
//! Exit code: 0 on success, 1 on any error.

use std::io::Read;

use lzsw::json::{escape_str, Json};
use lzsw::{base64, Decoder, Encoder};

const DEFAULT_MAX_OUTPUT: u64 = 64 * 1024 * 1024;

fn main() {
    let mut input = String::new();
    if let Err(e) = std::io::stdin().read_to_string(&mut input) {
        fail("IoError", &format!("cannot read stdin: {e}"));
    }
    match run(&input) {
        Ok(resp) => {
            println!("{resp}");
        }
        Err((kind, msg)) => fail(kind, &msg),
    }
}

fn fail(kind: &str, msg: &str) -> ! {
    println!(
        "{{\"ok\":false,\"error\":{{\"kind\":\"{}\",\"message\":\"{}\"}}}}",
        escape_str(kind),
        escape_str(msg)
    );
    std::process::exit(1);
}

fn run(input: &str) -> Result<String, (&'static str, String)> {
    let req = lzsw::json::parse(input).map_err(|e| ("BadRequest", format!("invalid JSON: {e}")))?;
    let op = req
        .get("op")
        .and_then(Json::as_str)
        .ok_or_else(|| ("BadRequest", "missing string field \"op\"".to_string()))?;
    let data_b64 = req
        .get("data")
        .and_then(Json::as_str)
        .ok_or_else(|| ("BadRequest", "missing string field \"data\"".to_string()))?;
    let data = base64::decode(data_b64).map_err(|e| ("BadRequest", format!("bad base64: {e}")))?;
    let chunk_size = req
        .get("chunk_size")
        .and_then(Json::as_u64)
        .map(|n| n.max(1) as usize);

    match op {
        "encode" => Ok(encode_op(&data, chunk_size)),
        "decode" => {
            let max_output = req
                .get("max_output")
                .and_then(Json::as_u64)
                .unwrap_or(DEFAULT_MAX_OUTPUT);
            decode_op(&data, max_output, chunk_size)
        }
        other => Err((
            "BadRequest",
            format!("unknown op \"{other}\", expected \"encode\" or \"decode\""),
        )),
    }
}

fn ok_response(op: &str, input_len: usize, output: &[u8]) -> String {
    format!(
        "{{\"ok\":true,\"op\":\"{}\",\"input_len\":{},\"output_len\":{},\"data\":\"{}\"}}",
        escape_str(op),
        input_len,
        output.len(),
        base64::encode(output)
    )
}

fn encode_op(data: &[u8], chunk_size: Option<usize>) -> String {
    let mut enc = Encoder::new();
    let mut out = Vec::new();
    feed(data, chunk_size, |chunk| {
        out.extend_from_slice(&enc.update(chunk));
    });
    out.extend_from_slice(&enc.finish());
    ok_response("encode", data.len(), &out)
}

fn decode_op(
    data: &[u8],
    max_output: u64,
    chunk_size: Option<usize>,
) -> Result<String, (&'static str, String)> {
    let mut dec = Decoder::new(max_output);
    let mut out = Vec::new();
    let mut result = Ok(());
    feed(data, chunk_size, |chunk| {
        if result.is_ok() {
            match dec.update(chunk) {
                Ok(bytes) => out.extend_from_slice(&bytes),
                Err(e) => result = Err(e),
            }
        }
    });
    result
        .and_then(|()| dec.finish())
        .map_err(|e| (e.kind(), e.to_string()))?;
    Ok(ok_response("decode", data.len(), &out))
}

/// Split `data` into chunks of `chunk_size` (or one chunk if None) and call
/// `f` for each. Empty input still triggers exactly one call.
fn feed(data: &[u8], chunk_size: Option<usize>, mut f: impl FnMut(&[u8])) {
    match chunk_size {
        Some(size) => {
            if data.is_empty() {
                f(&[]);
            }
            for chunk in data.chunks(size) {
                f(chunk);
            }
        }
        None => f(data),
    }
}
