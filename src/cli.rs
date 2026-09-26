//! JSON control entry point.
//!
//! The CLI is driven entirely by JSON request documents (a file path, or `-`
//! for stdin) and prints a single JSON response document to stdout:
//!
//! ```text
//! ecstripe encode  request.json
//! ecstripe decode  request.json
//! ecstripe info    request.json
//! ecstripe corrupt request.json
//! ```
//!
//! Successful responses: `{"status":"ok","data":{...}}`; failures:
//! `{"status":"error","error":{"kind":"...","message":"..."}}` with a non-zero
//! process exit code. See [`requests/`](../requests) for examples.

use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;

use serde::{Deserialize, Serialize};

use ecstripe::codec::{decode_stream, encode_stream, Limits, LossPattern};
use ecstripe::container::{Header, MAGIC, MAX_CHUNK_LEN, MAX_HEADER_LEN, TAG_CHUNK, TAG_EOS};
use ecstripe::error::Error;

/// Resource-budget fields shared by encode/decode requests.
#[derive(Debug, Deserialize)]
struct LimitsRequest {
    max_memory: Option<u64>,
    max_output: Option<u64>,
}

impl LimitsRequest {
    fn resolve(&self) -> Limits {
        let mut limits = Limits::default();
        if let Some(v) = self.max_memory {
            limits.max_memory = v;
        }
        if let Some(v) = self.max_output {
            limits.max_output = v;
        }
        limits
    }
}

#[derive(Debug, Deserialize)]
struct EncodeRequest {
    input: String,
    output: String,
    data_shards: u16,
    parity_shards: u16,
    stripe_size: u32,
    /// Optional; defaults to the input file's byte length.
    total_len: Option<u64>,
    #[serde(default)]
    tag_hash: bool,
    #[serde(default)]
    limits: Option<LimitsRequest>,
}

#[derive(Debug, Deserialize)]
struct LossRequest {
    #[serde(default)]
    shards: Vec<u8>,
    #[serde(default)]
    cells: Vec<(u32, u8)>,
}

#[derive(Debug, Deserialize)]
struct DecodeRequest {
    input: String,
    output: String,
    #[serde(default)]
    loss: Option<LossRequest>,
    #[serde(default)]
    expect_hash: bool,
    #[serde(default)]
    limits: Option<LimitsRequest>,
}

#[derive(Debug, Deserialize)]
struct InfoRequest {
    input: String,
}

/// One payload bit/byte flip applied by the `corrupt` helper.
#[derive(Debug, Clone, Copy, Deserialize)]
struct Flip {
    stripe: u32,
    shard: u8,
    /// Offset within the shard's payload bytes.
    offset: u64,
    /// Value XORed onto the target byte (default `0xff`).
    #[serde(default = "default_xor")]
    xor: u8,
}

fn default_xor() -> u8 {
    0xff
}

#[derive(Debug, Deserialize)]
struct CorruptRequest {
    input: String,
    output: String,
    flips: Vec<Flip>,
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "lowercase")]
pub enum Response {
    Ok { data: serde_json::Value },
    Error { error: ErrorBody },
}

#[derive(Debug, Serialize)]
pub(crate) struct ErrorBody {
    kind: &'static str,
    message: String,
}

fn error_kind(err: &Error) -> &'static str {
    match err {
        Error::Io(_) => "io",
        Error::InvalidParams(_) => "invalid_params",
        Error::BadMagic => "bad_magic",
        Error::UnsupportedVersion(_) => "unsupported_version",
        Error::BadHeader(_) => "bad_header",
        Error::BadRecord(_) => "bad_record",
        Error::LengthMismatch { .. } => "length_mismatch",
        Error::NotEnoughShards { .. } => "not_enough_shards",
        Error::SingularMatrix => "singular_matrix",
        Error::UnexpectedEos { .. } => "unexpected_eos",
        Error::OutputLimitExceeded { .. } => "output_limit_exceeded",
        Error::MemoryLimitExceeded { .. } => "memory_limit_exceeded",
        Error::HashMismatch { .. } => "hash_mismatch",
        // `Error` is #[non_exhaustive]; future variants map to a generic kind.
        _ => "error",
    }
}

fn respond(result: Result<serde_json::Value, Error>) -> Response {
    match result {
        Ok(data) => Response::Ok { data },
        Err(err) => Response::Error {
            error: ErrorBody {
                kind: error_kind(&err),
                message: err.to_string(),
            },
        },
    }
}

fn load_request<T: for<'de> Deserialize<'de>>(path: &str) -> Result<T, Error> {
    let bytes = if path == "-" {
        let mut buf = Vec::new();
        std::io::stdin().read_to_end(&mut buf).map_err(Error::Io)?;
        buf
    } else {
        fs::read(Path::new(&path))
            .map_err(|e| Error::InvalidParams(format!("cannot read request {path:?}: {e}")))?
    };
    serde_json::from_slice(&bytes)
        .map_err(|e| Error::InvalidParams(format!("malformed request JSON: {e}")))
}

fn file_len(path: &str) -> Result<u64, Error> {
    fs::metadata(Path::new(&path))
        .map(|m| m.len())
        .map_err(Error::Io)
}

fn run_encode(req: EncodeRequest) -> Result<serde_json::Value, Error> {
    let total_len = match req.total_len {
        Some(len) => len,
        None => file_len(&req.input)?,
    };
    let limits = req
        .limits
        .as_ref()
        .map_or_else(Limits::default, |l| l.resolve());
    let mut input = fs::File::open(&req.input).map_err(Error::Io)?;
    let mut output = fs::File::create(&req.output).map_err(Error::Io)?;

    let report = encode_stream(
        &mut input,
        &mut output,
        req.data_shards,
        req.parity_shards,
        req.stripe_size,
        total_len,
        req.tag_hash,
        limits,
    )?;
    Ok(serde_json::json!({
        "output": req.output,
        "stripes": report.stripes,
        "bytes": report.bytes,
        "sha256": report.sha256,
    }))
}

fn run_decode(req: DecodeRequest) -> Result<serde_json::Value, Error> {
    let limits = req
        .limits
        .as_ref()
        .map_or_else(Limits::default, |l| l.resolve());
    let loss = req
        .loss
        .as_ref()
        .map_or_else(LossPattern::default, |l| LossPattern {
            shards: l.shards.clone(),
            cells: l.cells.clone(),
        });
    let mut input = fs::File::open(&req.input).map_err(Error::Io)?;
    let mut output = fs::File::create(&req.output).map_err(Error::Io)?;

    let report = decode_stream(&mut input, &mut output, &loss, req.expect_hash, limits)?;
    Ok(serde_json::json!({
        "output": req.output,
        "stripes": report.stripes,
        "bytes": report.bytes,
        "sha256": report.sha256,
    }))
}

fn run_info(req: InfoRequest) -> Result<serde_json::Value, Error> {
    let bytes = fs::read(Path::new(&req.input)).map_err(Error::Io)?;
    let header = parse_header_only(&bytes)?;
    Ok(serde_json::to_value(&header).expect("header serializes"))
}

fn parse_header_only(bytes: &[u8]) -> Result<Header, Error> {
    if bytes.len() < 8 || &bytes[..4] != MAGIC {
        return Err(Error::BadMagic);
    }
    let header_len = u32::from_be_bytes(bytes[4..8].try_into().unwrap()) as usize;
    if header_len == 0 || header_len > MAX_HEADER_LEN as usize || 8 + header_len > bytes.len() {
        return Err(Error::BadHeader("invalid header length".into()));
    }
    let header: Header = serde_json::from_slice(&bytes[8..8 + header_len])?;
    header.validate()?;
    Ok(header)
}

/// Copy a container to a new file while XOR-flipping selected payload bytes.
/// The framing itself is left intact so the decoder accepts the stream and
/// reconstructs wrong-but-consistent data — demonstrating that silent
/// corruption requires an external integrity check.
fn run_corrupt(req: CorruptRequest) -> Result<serde_json::Value, Error> {
    let bytes = fs::read(Path::new(&req.input)).map_err(Error::Io)?;
    let mut output = fs::File::create(&req.output).map_err(Error::Io)?;

    // Index flips by (stripe, shard) -> offset -> xor value.
    let mut by_cell: BTreeMap<(u32, u8), BTreeMap<u64, u8>> = BTreeMap::new();
    for flip in &req.flips {
        by_cell
            .entry((flip.stripe, flip.shard))
            .or_default()
            .insert(flip.offset, flip.xor);
    }

    let mut cursor = bytes.as_slice();
    let total = copy_and_flip(&mut cursor, &mut output, &by_cell)?;
    output.flush()?;
    Ok(serde_json::json!({
        "output": req.output,
        "bytes": total,
        "flips_applied": req.flips.len(),
    }))
}

fn read_u32(input: &mut &[u8]) -> Result<u32, Error> {
    let mut buf = [0u8; 4];
    input
        .read_exact(&mut buf)
        .map_err(|_| Error::BadRecord("truncated container".into()))?;
    Ok(u32::from_be_bytes(buf))
}

fn copy_and_flip(
    input: &mut &[u8],
    output: &mut impl Write,
    flips: &BTreeMap<(u32, u8), BTreeMap<u64, u8>>,
) -> Result<u64, Error> {
    // Copy everything up to the record stream verbatim (magic + header).
    if input.len() < 8 {
        return Err(Error::BadMagic);
    }
    output.write_all(&input[..4])?;
    *input = &input[4..];
    let header_len = read_u32(input)? as usize;
    if header_len == 0 || header_len > MAX_HEADER_LEN as usize || input.len() < header_len {
        return Err(Error::BadHeader("invalid header length".into()));
    }
    output.write_all(&(header_len as u32).to_be_bytes())?;
    output.write_all(&input[..header_len])?;
    *input = &input[header_len..];

    let mut written: u64 = (4 + 4 + header_len) as u64;
    // Running payload cursor per (stripe, shard), since flips use offsets
    // absolute within a shard while a shard may span several chunk records.
    let mut cursors: BTreeMap<(u32, u8), u64> = BTreeMap::new();
    let mut applied: u64 = 0;

    loop {
        let mut tag = [0u8; 1];
        if input.read_exact(&mut tag).is_err() {
            break;
        }
        output.write_all(&tag)?;
        written += 1;
        match tag[0] {
            TAG_CHUNK => {
                let stripe = read_u32(input)?;
                let mut shard = [0u8; 1];
                input
                    .read_exact(&mut shard)
                    .map_err(|_| Error::BadRecord("truncated".into()))?;
                let chunk_len = read_u32(input)?;
                if chunk_len > MAX_CHUNK_LEN || input.len() < chunk_len as usize {
                    return Err(Error::BadRecord("invalid chunk length".into()));
                }
                output.write_all(&stripe.to_be_bytes())?;
                output.write_all(&shard)?;
                output.write_all(&chunk_len.to_be_bytes())?;

                let key = (stripe, shard[0]);
                let base = cursors.get(&key).copied().unwrap_or(0);
                let mut payload = input[..chunk_len as usize].to_vec();
                if let Some(offsets) = flips.get(&key) {
                    for (&offset, &xor) in offsets {
                        if offset < base {
                            return Err(Error::InvalidParams(format!(
                                "flip offset {offset} for {key:?} is before chunk start {base}"
                            )));
                        }
                        let local = (offset - base) as usize;
                        if local >= payload.len() {
                            continue; // lands in a later chunk (or out of range)
                        }
                        payload[local] ^= xor;
                        applied += 1;
                    }
                }
                output.write_all(&payload)?;
                *input = &input[chunk_len as usize..];
                cursors.insert(key, base + chunk_len as u64);
                written += chunk_len as u64 + 9;
            }
            TAG_EOS => {
                let count = read_u32(input)?;
                output.write_all(&count.to_be_bytes())?;
                written += 4;
                break;
            }
            other => return Err(Error::BadRecord(format!("unknown tag 0x{other:02x}"))),
        }
    }

    if applied != flips.values().map(|m| m.len() as u64).sum::<u64>() {
        return Err(Error::InvalidParams(format!(
            "only {applied} of {} requested flips landed inside shard payloads",
            flips.values().map(|m| m.len()).sum::<usize>()
        )));
    }
    Ok(written)
}

/// Run one CLI invocation. Returns the JSON response (the process layer sets
/// the exit code based on its status).
pub fn dispatch(args: &[String]) -> Response {
    let Some((command, request_path)) = parse_args(args) else {
        return respond(Err(Error::InvalidParams(
            "usage: ecstripe <encode|decode|info|corrupt> <request.json|- >".into(),
        )));
    };

    let result = match command.as_str() {
        "encode" => load_request::<EncodeRequest>(&request_path).and_then(run_encode),
        "decode" => load_request::<DecodeRequest>(&request_path).and_then(run_decode),
        "info" => load_request::<InfoRequest>(&request_path).and_then(run_info),
        "corrupt" => load_request::<CorruptRequest>(&request_path).and_then(run_corrupt),
        other => Err(Error::InvalidParams(format!("unknown command {other:?}"))),
    };
    respond(result)
}

fn parse_args(args: &[String]) -> Option<(String, String)> {
    match args {
        [command, path] => Some((command.clone(), path.clone())),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_header_only_roundtrips() {
        let header = Header::new(3, 2, 16, 40).unwrap();
        let json = serde_json::to_vec(&header).unwrap();
        let mut bytes = MAGIC.to_vec();
        bytes.extend_from_slice(&(json.len() as u32).to_be_bytes());
        bytes.extend_from_slice(&json);
        let parsed = parse_header_only(&bytes).unwrap();
        assert_eq!(parsed, header);
    }
}
