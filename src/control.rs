//! JSON control entry: parse one request object, run it, emit one response
//! object. Transport-agnostic — `main.rs` wires it to stdin/stdout, tests
//! call it directly.
//!
//! Request schema (one JSON object, discriminated by `op`):
//!
//! ```json
//! {
//!   "op": "encode",
//!   "data_shards": 4,                 // required, 1..=255
//!   "parity_shards": 2,               // required, 1..=255, k+m <= 255
//!   "stripe_size": 4096,              // required, 1..=16777216
//!   "input_path": "data.bin",         // required
//!   "output_path": "data.ecsr",       // required
//!   "max_stripe_memory": 67108864     // optional, bytes
//! }
//! ```
//!
//! ```json
//! {
//!   "op": "decode",
//!   "input_path": "data.ecsr",        // required
//!   "output_path": "recovered.bin",   // required
//!   "missing_shards": [1, 4],         // optional, known erasures
//!   "max_output_bytes": 1073741824,   // optional, decode output cap
//!   "expected_sha256": "<hex>"        // optional, external integrity check
//! }
//! ```
//!
//! Response: `{ "ok": true, "op": ..., ...stats }` or
//! `{ "ok": false, "op": ..., "error": "..." }`. Decode responses also carry
//! `sha256` (hex of the decoded output) and `verified` (null when no
//! `expected_sha256` was given).

use crate::codec::{self, EncodeParams};
use crate::error::{Error, Result};
use crate::format::{DEFAULT_MAX_OUTPUT_BYTES, DEFAULT_MAX_STRIPE_MEMORY};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufReader, BufWriter};

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "lowercase")]
pub enum Request {
    Encode {
        data_shards: u8,
        parity_shards: u8,
        stripe_size: u32,
        input_path: String,
        output_path: String,
        #[serde(default = "default_stripe_memory")]
        max_stripe_memory: u64,
    },
    Decode {
        input_path: String,
        output_path: String,
        #[serde(default)]
        missing_shards: Vec<u32>,
        #[serde(default = "default_max_output")]
        max_output_bytes: u64,
        #[serde(default)]
        expected_sha256: Option<String>,
    },
}

fn default_stripe_memory() -> u64 {
    DEFAULT_MAX_STRIPE_MEMORY
}

fn default_max_output() -> u64 {
    DEFAULT_MAX_OUTPUT_BYTES
}

/// Parse a JSON request string.
pub fn parse_request(input: &str) -> Result<Request> {
    serde_json::from_str(input).map_err(|e| Error::BadRequest(e.to_string()))
}

/// Execute one request, returning the JSON response value. This function
/// never fails to produce a response: operational errors are reported as
/// `{ "ok": false, ... }`.
pub fn run(request: &Request) -> Value {
    match request {
        Request::Encode { .. } => match run_encode(request) {
            Ok(v) => v,
            Err(e) => json!({ "ok": false, "op": "encode", "error": e.to_string() }),
        },
        Request::Decode { .. } => match run_decode(request) {
            Ok(v) => v,
            Err(e) => json!({ "ok": false, "op": "decode", "error": e.to_string() }),
        },
    }
}

/// Convenience wrapper: JSON string in, JSON string out.
pub fn run_json(input: &str) -> String {
    match parse_request(input) {
        Ok(req) => run(&req).to_string(),
        Err(e) => json!({ "ok": false, "error": e.to_string() }).to_string(),
    }
}

fn run_encode(request: &Request) -> Result<Value> {
    let Request::Encode {
        data_shards,
        parity_shards,
        stripe_size,
        input_path,
        output_path,
        max_stripe_memory,
    } = request
    else {
        unreachable!()
    };

    let params = EncodeParams {
        data_shards: *data_shards,
        parity_shards: *parity_shards,
        stripe_size: *stripe_size,
        max_stripe_memory: *max_stripe_memory,
    };
    // Validate before touching the filesystem so bad params fail fast.
    crate::format::Header {
        data_shards: params.data_shards,
        parity_shards: params.parity_shards,
        stripe_size: params.stripe_size,
        original_len: 0,
    }
    .validate(params.max_stripe_memory)?;

    let input_len = std::fs::metadata(input_path)
        .map_err(|e| Error::BadRequest(format!("cannot stat input_path {input_path}: {e}")))?
        .len();

    let reader = BufReader::new(File::open(input_path)?);
    let writer = BufWriter::new(File::create(output_path)?);
    let stats = codec::encode(&params, input_len, reader, writer)?;

    Ok(json!({
        "ok": true,
        "op": "encode",
        "input_bytes": stats.input_bytes,
        "output_bytes": stats.output_bytes,
        "stripes": stats.stripes,
        "data_shards": data_shards,
        "parity_shards": parity_shards,
        "stripe_size": stripe_size,
    }))
}

fn run_decode(request: &Request) -> Result<Value> {
    let Request::Decode {
        input_path,
        output_path,
        missing_shards,
        max_output_bytes,
        expected_sha256,
    } = request
    else {
        unreachable!()
    };

    let reader = BufReader::new(File::open(input_path)?);
    let writer = BufWriter::new(File::create(output_path)?);
    // Hash the decoded stream as it passes through, so the external
    // integrity check costs no extra pass and no extra memory.
    let mut hashing_writer = HashingWriter {
        inner: writer,
        hasher: Sha256::new(),
    };
    let decode_result = codec::decode(reader, &mut hashing_writer, missing_shards, *max_output_bytes);
    let sha256_hex = hex_encode(&hashing_writer.hasher.finalize_reset());
    let stats = decode_result?;

    let verified = match expected_sha256 {
        None => Value::Null,
        Some(expected) => {
            if !expected.eq_ignore_ascii_case(&sha256_hex) {
                return Err(Error::ChecksumMismatch {
                    expected: expected.clone(),
                    actual: sha256_hex,
                });
            }
            Value::Bool(true)
        }
    };

    Ok(json!({
        "ok": true,
        "op": "decode",
        "output_bytes": stats.output_bytes,
        "stripes": stats.stripes,
        "reconstructed_shards": stats.reconstructed_shards,
        "sha256": sha256_hex,
        "verified": verified,
    }))
}

struct HashingWriter<W: std::io::Write> {
    inner: W,
    hasher: Sha256,
}

impl<W: std::io::Write> std::io::Write for HashingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hasher.update(&buf[..n]);
        Ok(n)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}
