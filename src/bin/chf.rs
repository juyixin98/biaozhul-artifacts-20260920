//! `chf` — JSON-controlled canonical Huffman compressor/decompressor.
//!
//! Usage: `chf <request.json>` or `chf -` to read the request from stdin.
//! The response is a single JSON object on stdout; exit code 0 on success,
//! 1 on failure (the response then carries an `error` field).

use canohuff::decode::{DecodeOptions, IncompletePolicy, StreamDecoder};
use canohuff::encode::{count_frequencies, StreamEncoder, CHUNK, DEFAULT_MAX_INPUT_BYTES};
use canohuff::decode::DEFAULT_MAX_OUTPUT_BYTES;
use canohuff::json::{escape_json, parse_object, JsonValue};
use canohuff::table::{build_lengths, CodeTable};
use canohuff::{Error, Result};
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::time::Instant;

struct Request {
    op: String,
    input: String,
    output: String,
    max_input_bytes: u64,
    max_output_bytes: u64,
    incomplete_policy: IncompletePolicy,
}

fn main() {
    std::process::exit(real_main());
}

fn real_main() -> i32 {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: chf <request.json | ->");
        return 2;
    }
    if args[1] == "--help" || args[1] == "-h" {
        println!("usage: chf <request.json | ->");
        return 0;
    }
    let text = if args[1] == "-" {
        let mut s = String::new();
        if let Err(e) = std::io::stdin().read_to_string(&mut s) {
            println!("{}", error_response(None, &Error::Io(e)));
            return 1;
        }
        s
    } else {
        match std::fs::read_to_string(&args[1]) {
            Ok(s) => s,
            Err(e) => {
                println!("{}", error_response(None, &Error::Io(e)));
                return 1;
            }
        }
    };

    // Parse the op first so error responses can echo it.
    let op_hint = parse_object(&text)
        .ok()
        .and_then(|m| match m.get("op") {
            Some(JsonValue::Str(s)) => Some(s.clone()),
            _ => None,
        });

    match run(&text) {
        Ok(resp) => {
            println!("{resp}");
            0
        }
        Err(e) => {
            println!("{}", error_response(op_hint.as_deref(), &e));
            1
        }
    }
}

fn run(text: &str) -> Result<String> {
    let req = parse_request(text)?;
    let start = Instant::now();
    let (input_bytes, output_bytes) = match req.op.as_str() {
        "compress" => do_compress(&req)?,
        "decompress" => do_decompress(&req)?,
        other => {
            return Err(Error::BadRequest(format!(
                "unknown op '{other}': expected 'compress' or 'decompress'"
            )))
        }
    };
    let elapsed_ms = start.elapsed().as_millis() as u64;
    let ratio = if input_bytes > 0 {
        format!("{:.4}", output_bytes as f64 / input_bytes as f64)
    } else {
        "null".to_string()
    };
    Ok(format!(
        "{{\"ok\":true,\"op\":\"{}\",\"input_bytes\":{},\"output_bytes\":{},\"ratio\":{},\"elapsed_ms\":{}}}",
        escape_json(&req.op),
        input_bytes,
        output_bytes,
        ratio,
        elapsed_ms
    ))
}

fn error_response(op: Option<&str>, e: &Error) -> String {
    let op_field = match op {
        Some(op) => format!("\"op\":\"{}\",", escape_json(op)),
        None => String::new(),
    };
    format!(
        "{{\"ok\":false,{}\"error\":\"{}\"}}",
        op_field,
        escape_json(&e.to_string())
    )
}

fn parse_request(text: &str) -> Result<Request> {
    let map = parse_object(text)?;
    let get_str = |key: &str| -> Result<String> {
        match map.get(key) {
            Some(JsonValue::Str(s)) => Ok(s.clone()),
            Some(_) => Err(Error::BadRequest(format!("field '{key}' must be a string"))),
            None => Err(Error::BadRequest(format!("missing field '{key}'"))),
        }
    };
    let get_num = |key: &str, default: u64| -> Result<u64> {
        match map.get(key) {
            Some(JsonValue::Num(n)) => Ok(*n),
            Some(_) => Err(Error::BadRequest(format!("field '{key}' must be a number"))),
            None => Ok(default),
        }
    };
    let incomplete_policy = match map.get("incomplete_policy") {
        None => IncompletePolicy::Permit,
        Some(JsonValue::Str(s)) if s == "permit" => IncompletePolicy::Permit,
        Some(JsonValue::Str(s)) if s == "reject" => IncompletePolicy::Reject,
        Some(_) => {
            return Err(Error::BadRequest(
                "field 'incomplete_policy' must be \"permit\" or \"reject\"".into(),
            ))
        }
    };
    Ok(Request {
        op: get_str("op")?,
        input: get_str("input")?,
        output: get_str("output")?,
        max_input_bytes: get_num("max_input_bytes", DEFAULT_MAX_INPUT_BYTES)?,
        max_output_bytes: get_num("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)?,
        incomplete_policy,
    })
}

/// Two-pass streaming compress: pass 1 counts frequencies, pass 2 encodes.
/// Returns (input_bytes, output_bytes).
fn do_compress(req: &Request) -> Result<(u64, u64)> {
    let (freqs, total) = count_frequencies(
        BufReader::new(File::open(&req.input)?),
        req.max_input_bytes,
    )?;
    let table = CodeTable::from_lengths(build_lengths(&freqs))?;

    let mut reader = BufReader::new(File::open(&req.input)?);
    let writer = BufWriter::new(File::create(&req.output)?);
    let mut enc = StreamEncoder::new(writer, table, total)?;
    let mut buf = [0u8; CHUNK];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        enc.write_chunk(&buf[..n])?;
    }
    let mut w = enc.finish()?;
    w.flush()?;
    let out_len = std::fs::metadata(&req.output)?.len();
    Ok((total, out_len))
}

/// Streaming decompress with header validation and output limit.
/// Returns (input_bytes, output_bytes).
fn do_decompress(req: &Request) -> Result<(u64, u64)> {
    let in_len = std::fs::metadata(&req.input)?.len();
    let reader = BufReader::new(File::open(&req.input)?);
    let opts = DecodeOptions {
        max_output_bytes: req.max_output_bytes,
        incomplete_policy: req.incomplete_policy,
    };
    let mut dec = StreamDecoder::new(reader, &opts)?;
    let mut writer = BufWriter::new(File::create(&req.output)?);
    let mut total = 0u64;
    let mut buf = [0u8; CHUNK];
    loop {
        let n = dec.decode_chunk(&mut buf)?;
        if n == 0 {
            break;
        }
        writer.write_all(&buf[..n])?;
        total += n as u64;
    }
    dec.finish()?;
    writer.flush()?;
    Ok((in_len, total))
}
