//! incutf8 — JSON control entry point for the incremental UTF-8 codec.
//!
//! Reads one JSON request object (from stdin, or `--request FILE`),
//! writes one JSON response object to stdout. See README.md for the
//! protocol specification.
//!
//! Exit codes: 0 = ok, 1 = request processed but decoding/encoding
//! reported errors, 2 = malformed request or I/O failure.

use std::io::Read;
use std::process::ExitCode;

use incutf8::decoder::{Decoder, ErrorPolicy, DEFAULT_MAX_OUTPUT_CODEPOINTS};
use incutf8::error::{DecodeError, EncodeError};
use incutf8::json::{self, Json};
use incutf8::{base64, encoder};

/// Default cap on the request size, to bound memory use (32 MiB).
const DEFAULT_MAX_REQUEST_BYTES: u64 = 32 * 1024 * 1024;

const EXIT_OK: u8 = 0;
const EXIT_CODEC_ERRORS: u8 = 1;
const EXIT_BAD_REQUEST: u8 = 2;

fn main() -> ExitCode {
    let code = run();
    ExitCode::from(code)
}

fn run() -> u8 {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut request_path: Option<String> = None;
    let mut max_request_bytes = DEFAULT_MAX_REQUEST_BYTES;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--request" | "-r" => {
                i += 1;
                match args.get(i) {
                    Some(path) => request_path = Some(path.clone()),
                    None => return fail_request("missing value for --request"),
                }
            }
            "--max-request-bytes" => {
                i += 1;
                match args.get(i).and_then(|s| s.parse::<u64>().ok()) {
                    Some(n) => max_request_bytes = n,
                    None => return fail_request("missing or invalid value for --max-request-bytes"),
                }
            }
            "--help" | "-h" => {
                print_usage();
                return EXIT_OK;
            }
            other => return fail_request(&format!("unknown argument: {}", other)),
        }
        i += 1;
    }

    let input = match read_request(request_path.as_deref(), max_request_bytes) {
        Ok(bytes) => bytes,
        Err(message) => return fail_request(&message),
    };
    let text = match String::from_utf8(input) {
        Ok(t) => t,
        Err(_) => return fail_request("request is not valid UTF-8"),
    };
    let request = match json::parse(&text) {
        Ok(v) => v,
        Err(e) => return fail_request(&format!("invalid JSON: {}", e)),
    };

    let op = match request.get("op").and_then(Json::as_str) {
        Some(op) => op.to_string(),
        None => return fail_request("missing or invalid \"op\" (expected a string)"),
    };

    let (response, code) = match op.as_str() {
        "ping" => (
            json::obj(vec![
                ("ok", Json::Bool(true)),
                ("op", Json::Str("ping".to_string())),
                ("version", Json::Str(env!("CARGO_PKG_VERSION").to_string())),
            ]),
            EXIT_OK,
        ),
        "decode" => op_decode(&request, false),
        "validate" => op_decode(&request, true),
        "encode" => op_encode(&request),
        other => {
            return fail_request(&format!(
                "unknown op \"{}\" (expected ping|decode|validate|encode)",
                other
            ))
        }
    };
    println!("{}", json::to_string(&response));
    code
}

fn print_usage() {
    eprintln!(
        "usage: incutf8 [--request FILE] [--max-request-bytes N]\n\
         reads one JSON request (stdin by default), writes one JSON response to stdout"
    );
}

fn fail_request(message: &str) -> u8 {
    let response = json::obj(vec![
        ("ok", Json::Bool(false)),
        ("error", Json::Str(message.to_string())),
    ]);
    println!("{}", json::to_string(&response));
    EXIT_BAD_REQUEST
}

fn read_request(path: Option<&str>, max_bytes: u64) -> Result<Vec<u8>, String> {
    let mut buf = Vec::new();
    let reader: Box<dyn Read> = match path {
        Some(p) => Box::new(
            std::fs::File::open(p).map_err(|e| format!("cannot open {}: {}", p, e))?,
        ),
        None => Box::new(std::io::stdin()),
    };
    let mut limited = reader.take(max_bytes.saturating_add(1));
    limited
        .read_to_end(&mut buf)
        .map_err(|e| format!("failed to read request: {}", e))?;
    if buf.len() as u64 > max_bytes {
        return Err(format!(
            "request exceeds the limit of {} bytes (use --max-request-bytes to raise it)",
            max_bytes
        ));
    }
    Ok(buf)
}

/// Handle `decode` and `validate` ops.
fn op_decode(request: &Json, validate_only: bool) -> (Json, u8) {
    // Input: either "chunks": [base64, ...] or a single "data": base64.
    let chunk_strings: Vec<String> = match (request.get("chunks"), request.get("data")) {
        (Some(Json::Arr(items)), None) => {
            let mut v = Vec::with_capacity(items.len());
            for (idx, item) in items.iter().enumerate() {
                match item.as_str() {
                    Some(s) => v.push(s.to_string()),
                    None => {
                        return bad_request_field(&format!("chunks[{}] is not a string", idx))
                    }
                }
            }
            v
        }
        (None, Some(data)) => match data.as_str() {
            Some(s) => vec![s.to_string()],
            None => return bad_request_field("\"data\" is not a base64 string"),
        },
        (Some(_), None) => return bad_request_field("\"chunks\" must be an array of base64 strings"),
        (None, None) => return bad_request_field("missing input: provide \"chunks\" or \"data\""),
        (Some(_), Some(_)) => {
            return bad_request_field("provide either \"chunks\" or \"data\", not both")
        }
    };

    let is_final = request.get("final").and_then(Json::as_bool).unwrap_or(true);
    let policy = match request.get("error_policy") {
        None => ErrorPolicy::Abort,
        Some(Json::Str(s)) if s == "abort" => ErrorPolicy::Abort,
        Some(Json::Str(s)) if s == "collect" => ErrorPolicy::Collect,
        Some(_) => return bad_request_field("error_policy must be \"abort\" or \"collect\""),
    };
    let max_output = request
        .get("max_output_codepoints")
        .and_then(Json::as_u64)
        .unwrap_or(DEFAULT_MAX_OUTPUT_CODEPOINTS);
    let emit_output = !validate_only
        && request.get("emit_output").and_then(Json::as_bool).unwrap_or(true);

    let mut decoder = Decoder::with_options(policy, max_output, emit_output);
    for (idx, chunk_b64) in chunk_strings.iter().enumerate() {
        let chunk = match base64::decode(chunk_b64) {
            Ok(c) => c,
            Err(e) => {
                return bad_request_field(&format!("chunks[{}]: {}", idx, e));
            }
        };
        decoder.feed(&chunk);
    }
    let report = if is_final { decoder.finish() } else { decoder.finish_non_final() };

    let mut members: Vec<(&str, Json)> = vec![
        ("ok", Json::Bool(report.ok())),
        ("op", Json::Str(if validate_only { "validate" } else { "decode" }.to_string())),
        ("final", Json::Bool(is_final)),
        ("consumed_bytes", json::num(report.consumed_bytes)),
        ("output_codepoints", json::num(report.output_codepoints)),
        ("truncated", Json::Bool(report.truncated)),
        ("pending_sequence_bytes", json::num(u64::from(report.pending_sequence_bytes))),
    ];
    if emit_output {
        match String::from_utf8(report.output.clone()) {
            Ok(text) => members.push(("output_text", Json::Str(text))),
            Err(_) => members.push(("output_base64", Json::Str(base64::encode(&report.output)))),
        }
    }
    members.push((
        "errors",
        Json::Arr(report.errors.iter().map(decode_error_json).collect()),
    ));
    let code = if report.ok() { EXIT_OK } else { EXIT_CODEC_ERRORS };
    (json::obj(members), code)
}

/// Handle the `encode` op.
fn op_encode(request: &Json) -> (Json, u8) {
    let codepoints: Vec<u32> = match (request.get("text"), request.get("codepoints")) {
        (Some(Json::Str(s)), None) => return encode_bytes_response(encoder::encode_str(s), vec![]),
        (None, Some(Json::Arr(items))) => {
            let mut v = Vec::with_capacity(items.len());
            for (idx, item) in items.iter().enumerate() {
                match item.as_u64() {
                    Some(n) if n <= u64::from(u32::MAX) => v.push(n as u32),
                    _ => {
                        return bad_request_field(&format!(
                            "codepoints[{}] is not a non-negative integer",
                            idx
                        ))
                    }
                }
            }
            v
        }
        (None, None) => return bad_request_field("missing input: provide \"text\" or \"codepoints\""),
        (Some(_), Some(_)) => {
            return bad_request_field("provide either \"text\" or \"codepoints\", not both")
        }
        (Some(_), None) => return bad_request_field("\"text\" must be a string"),
        (None, Some(_)) => return bad_request_field("\"codepoints\" must be an array of integers"),
    };

    let mut out = Vec::new();
    let mut errors = Vec::new();
    for (idx, cp) in codepoints.iter().enumerate() {
        if let Err(e) = encoder::encode_codepoint(*cp, &mut out) {
            errors.push(encode_error_json(idx as u64, &e));
        }
    }
    encode_bytes_response(out, errors)
}

fn encode_bytes_response(bytes: Vec<u8>, errors: Vec<Json>) -> (Json, u8) {
    let ok = errors.is_empty();
    let response = json::obj(vec![
        ("ok", Json::Bool(ok)),
        ("op", Json::Str("encode".to_string())),
        ("data_base64", Json::Str(base64::encode(&bytes))),
        ("bytes", json::num(bytes.len() as u64)),
        ("errors", Json::Arr(errors)),
    ]);
    (response, if ok { EXIT_OK } else { EXIT_CODEC_ERRORS })
}

fn decode_error_json(e: &DecodeError) -> Json {
    let mut members: Vec<(&str, Json)> = vec![
        ("kind", Json::Str(e.kind.as_str().to_string())),
        ("offset", json::num(e.offset)),
        ("sequence_start", json::num(e.sequence_start)),
        ("sequence_len", json::num(u64::from(e.sequence_len))),
        ("detail", Json::Str(e.detail.clone())),
    ];
    if let Some(b) = e.byte {
        members.push(("byte_hex", Json::Str(format!("0x{:02X}", b))));
    }
    json::obj(members)
}

fn encode_error_json(index: u64, e: &EncodeError) -> Json {
    json::obj(vec![
        ("kind", Json::Str(e.kind.as_str().to_string())),
        ("index", json::num(index)),
        ("codepoint", json::num(u64::from(e.codepoint))),
        ("codepoint_hex", Json::Str(format!("U+{:04X}", e.codepoint))),
        ("detail", Json::Str(e.to_string())),
    ])
}

fn bad_request_field(message: &str) -> (Json, u8) {
    (
        json::obj(vec![
            ("ok", Json::Bool(false)),
            ("error", Json::Str(message.to_string())),
        ]),
        EXIT_BAD_REQUEST,
    )
}
