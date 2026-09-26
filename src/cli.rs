//! Command-line interface and JSON control plane.
//!
//! Two equivalent ways to drive the codec:
//!
//! 1. Subcommands: `encode`, `decode`, `forward`, `fingerprint`.
//! 2. A single JSON **request envelope** via `bschema request`
//!    (see `examples/requests/`), which is the JSON control entry point:
//!    everything is one JSON document in and one JSON document out.

use std::fs;
use std::io::{self, Read as _, Write as _};
use std::process::ExitCode;

use crate::base64;
use crate::error::Result;
use crate::fingerprint;
use crate::json::{self, Json};
use crate::limits::Limits;
use crate::reader;
use crate::schema::Schema;
use crate::value;
use crate::writer;

/// CLI entry point. Returns a process exit code.
pub fn run(args: &[String]) -> ExitCode {
    match run_inner(args) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(1)
        }
    }
}

fn run_inner(args: &[String]) -> Result<ExitCode> {
    let (cmd, rest) = match args.split_first() {
        Some((c, r)) => (c.as_str(), r),
        None => {
            print_usage();
            return Ok(ExitCode::from(2));
        }
    };

    match cmd {
        "encode" => cmd_encode(rest),
        "decode" => cmd_decode(rest),
        "forward" => cmd_forward(rest),
        "fingerprint" => cmd_fingerprint(rest),
        "request" => cmd_request(rest),
        "version" | "--version" | "-V" => {
            println!("bschema {}", env!("CARGO_PKG_VERSION"));
            Ok(ExitCode::SUCCESS)
        }
        "help" | "--help" | "-h" => {
            print_usage();
            Ok(ExitCode::SUCCESS)
        }
        other => {
            eprintln!("unknown command `{other}`");
            print_usage();
            Ok(ExitCode::from(2))
        }
    }
}

fn print_usage() {
    eprintln!(
        "bschema {v} — BSE1 binary schema-evolution codec\n\
\n\
USAGE:\n  bschema <COMMAND> [OPTIONS]\n\
\n\
COMMANDS:\n  encode       Encode a JSON message to BSE1 binary\n  decode       Decode BSE1 binary to a JSON message\n\
  forward      Decode and re-encode (transparent proxy; unknown fields survive)\n\
  fingerprint  Print a schema's wire-contract fingerprint (FNV-1a64, hex)\n\
  request      Execute a JSON request envelope (control plane)\n\
  version      Print version\n\
\n\
ENCODE/DECODE/FORWARD OPTIONS:\n\
  --schema <FILE>     Schema JSON document (required)\n\
  --message <FILE>    JSON message for encode (default: stdin)\n\
  --input <FILE>      Binary input for decode/forward (default: stdin)\n\
  --out <FILE>        Output file (default: stdout)\n\
  --base64            Binary I/O as standard base64 text instead of raw bytes\n\
  --meta              decode: wrap output with fingerprint metadata\n\
  --max-bytes <N>     Body input/output byte limit (default 67108864)\n\
  --max-depth <N>     Nested-message depth limit (default 64)\n\
  --max-repeated <N>  Max values per repeated field (default 1000000)\n\
\n\
REQUEST:\n  bschema request [--file <FILE>]   (default: read request JSON from stdin)",
        v = env!("CARGO_PKG_VERSION")
    );
}

// ---------------------------------------------------------------------------
// Flag parsing
// ---------------------------------------------------------------------------

struct Flags {
    values: Vec<(String, String)>,
}

impl Flags {
    fn parse(args: &[String]) -> Flags {
        let mut values = Vec::new();
        let mut i = 0;
        while i < args.len() {
            let a = &args[i];
            if let Some(flag) = a.strip_prefix("--") {
                if let Some(eq) = flag.find('=') {
                    values.push((flag[..eq].to_string(), flag[eq + 1..].to_string()));
                    i += 1;
                } else if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                    values.push((flag.to_string(), args[i + 1].clone()));
                    i += 2;
                } else {
                    values.push((flag.to_string(), String::new()));
                    i += 1;
                }
            } else {
                i += 1;
            }
        }
        Flags { values }
    }

    fn get(&self, key: &str) -> Option<&str> {
        self.values
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    fn present(&self, key: &str) -> bool {
        self.values.iter().any(|(k, _)| k == key)
    }
}

fn load_schema(path: &str) -> Result<Schema> {
    let text = fs::read_to_string(path)?;
    let doc = json::parse(&text)?;
    Schema::from_json(&doc)
}

fn limits_from_flags(f: &Flags) -> Result<Limits> {
    let mut l = Limits::default();
    if let Some(v) = f.get("max-bytes") {
        let n: u64 = v
            .parse()
            .map_err(|_| crate::Error::Json(format!("invalid --max-bytes value `{v}`")))?;
        l.max_message_bytes = n;
        l.max_output_bytes = n;
    }
    if let Some(v) = f.get("max-depth") {
        l.max_nesting = v
            .parse()
            .map_err(|_| crate::Error::Json(format!("invalid --max-depth value `{v}`")))?;
    }
    if let Some(v) = f.get("max-repeated") {
        l.max_repeated = v
            .parse()
            .map_err(|_| crate::Error::Json(format!("invalid --max-repeated value `{v}`")))?;
    }
    Ok(l)
}

fn read_all(path: Option<&str>) -> Result<Vec<u8>> {
    match path {
        Some(p) => Ok(fs::read(p)?),
        None => {
            let mut buf = Vec::new();
            io::stdin().read_to_end(&mut buf)?;
            Ok(buf)
        }
    }
}

fn write_output(path: Option<&str>, bytes: &[u8]) -> Result<()> {
    match path {
        Some(p) => Ok(fs::write(p, bytes)?),
        None => {
            io::stdout().write_all(bytes)?;
            Ok(())
        }
    }
}

// ---------------------------------------------------------------------------
// Subcommands
// ---------------------------------------------------------------------------

fn cmd_encode(args: &[String]) -> Result<ExitCode> {
    let f = Flags::parse(args);
    let schema = require_schema(&f)?;
    let limits = limits_from_flags(&f)?;

    let raw = read_all(f.get("message").or(f.get("input")))?;
    let text = String::from_utf8(raw)?;
    let msg_json = json::parse(&text)?;
    let msg = value::json_to_message(&schema, &msg_json)?;
    let bin = writer::encode(&schema, &msg, &limits)?;

    let out = if f.present("base64") {
        base64::encode(&bin).into_bytes()
    } else {
        bin
    };
    write_output(f.get("out"), &out)?;
    Ok(ExitCode::SUCCESS)
}

fn cmd_decode(args: &[String]) -> Result<ExitCode> {
    let f = Flags::parse(args);
    let schema = require_schema(&f)?;
    let limits = limits_from_flags(&f)?;

    let mut raw = read_all(f.get("input"))?;
    if f.present("base64") {
        let s = std::str::from_utf8(&raw)?.trim().to_string();
        raw = base64::decode(&s)?;
    }
    let decoded = reader::decode(&schema, &raw, &limits)?;
    let message_json = value::message_to_json(&schema, &decoded.message);

    let output = if f.present("meta") {
        Json::Object(vec![
            ("ok".into(), Json::Bool(true)),
            (
                "wire_fingerprint".into(),
                Json::Str(fingerprint::to_hex(decoded.wire_fingerprint)),
            ),
            (
                "schema_fingerprint".into(),
                Json::Str(fingerprint::to_hex(decoded.schema_fingerprint)),
            ),
            (
                "fingerprint_match".into(),
                Json::Bool(decoded.fingerprint_match()),
            ),
            ("message".into(), message_json),
        ])
    } else {
        message_json
    };
    let text = json::to_string_pretty(&output)?;
    write_output(f.get("out"), text.as_bytes())?;
    Ok(ExitCode::SUCCESS)
}

fn cmd_forward(args: &[String]) -> Result<ExitCode> {
    let f = Flags::parse(args);
    let schema = require_schema(&f)?;
    let limits = limits_from_flags(&f)?;

    let mut raw = read_all(f.get("input"))?;
    if f.present("base64") {
        let s = std::str::from_utf8(&raw)?.trim().to_string();
        raw = base64::decode(&s)?;
    }
    // Decode against the reader's schema (which may be newer/older); unknown
    // fields are retained, then re-emitted on encode.
    let decoded = reader::decode(&schema, &raw, &limits)?;
    let bin = writer::encode(&schema, &decoded.message, &limits)?;

    let out = if f.present("base64") {
        base64::encode(&bin).into_bytes()
    } else {
        bin
    };
    write_output(f.get("out"), &out)?;
    Ok(ExitCode::SUCCESS)
}

fn cmd_fingerprint(args: &[String]) -> Result<ExitCode> {
    let f = Flags::parse(args);
    let schema = require_schema(&f)?;
    println!("{}", fingerprint::to_hex(fingerprint::fingerprint(&schema)));
    Ok(ExitCode::SUCCESS)
}

fn require_schema(f: &Flags) -> Result<Schema> {
    match f.get("schema") {
        Some(p) => load_schema(p),
        None => Err(crate::Error::Json(
            "missing required --schema <FILE>".into(),
        )),
    }
}

// ---------------------------------------------------------------------------
// JSON request envelope (control plane)
// ---------------------------------------------------------------------------

fn cmd_request(args: &[String]) -> Result<ExitCode> {
    let f = Flags::parse(args);
    let raw = read_all(f.get("file"))?;
    let text = String::from_utf8(raw)?;
    let req = json::parse(&text)?;

    let resp = handle_request(&req)?;
    println!("{}", json::to_string_pretty(&resp)?);
    Ok(ExitCode::SUCCESS)
}

/// Execute one JSON request; public for integration tests.
pub fn handle_request(req: &Json) -> Result<Json> {
    let op = req
        .get("op")
        .and_then(|v| v.as_str())
        .ok_or_else(|| crate::Error::Json("request needs string `op`".into()))?;

    match op {
        "encode" => req_encode(req),
        "decode" => req_decode(req),
        "forward" => req_forward(req),
        "fingerprint" => req_fingerprint(req),
        other => Err(crate::Error::Json(format!(
            "unknown op `{other}` (expected encode|decode|forward|fingerprint)"
        ))),
    }
}

fn req_schema(req: &Json) -> Result<(Schema, Limits)> {
    let doc = match req.get("schema") {
        Some(d) => d.clone(),
        None => match req.get("schema_file") {
            Some(Json::Str(p)) => json::parse(&fs::read_to_string(p)?)?,
            _ => {
                return Err(crate::Error::Json(
                    "request needs `schema` object or `schema_file`".into(),
                ))
            }
        },
    };
    let schema = Schema::from_json(&doc)?;

    let mut limits = Limits::default();
    if let Some(l) = req.get("limits").and_then(|v| v.as_object()) {
        if let Some(n) = l
            .iter()
            .find(|(k, _)| k == "max_message_bytes")
            .and_then(|(_, v)| v.as_u64())
        {
            limits.max_message_bytes = n;
        }
        if let Some(n) = l
            .iter()
            .find(|(k, _)| k == "max_output_bytes")
            .and_then(|(_, v)| v.as_u64())
        {
            limits.max_output_bytes = n;
        }
        if let Some(n) = l
            .iter()
            .find(|(k, _)| k == "max_nesting")
            .and_then(|(_, v)| v.as_u64())
        {
            limits.max_nesting = n as u32;
        }
        if let Some(n) = l
            .iter()
            .find(|(k, _)| k == "max_repeated")
            .and_then(|(_, v)| v.as_u64())
        {
            limits.max_repeated = n as usize;
        }
    }
    Ok((schema, limits))
}

/// Request binary input: explicit `input_base64` wins, else `input_file`.
fn req_input_bytes(req: &Json) -> Result<Vec<u8>> {
    if let Some(Json::Str(s)) = req.get("input_base64") {
        return base64::decode(s);
    }
    if let Some(Json::Str(p)) = req.get("input_file") {
        return Ok(fs::read(p)?);
    }
    Err(crate::Error::Json(
        "request needs `input_base64` or `input_file`".into(),
    ))
}

fn maybe_write(req: &Json, bytes: &[u8]) -> Result<Json> {
    if let Some(Json::Str(p)) = req.get("output_file") {
        fs::write(p, bytes)?;
        Ok(Json::Object(vec![
            ("ok".into(), Json::Bool(true)),
            ("output_file".into(), Json::Str(p.clone())),
            ("output_bytes".into(), Json::UInt(bytes.len() as u64)),
        ]))
    } else {
        Ok(Json::Object(vec![
            ("ok".into(), Json::Bool(true)),
            ("output_base64".into(), Json::Str(base64::encode(bytes))),
            ("output_bytes".into(), Json::UInt(bytes.len() as u64)),
        ]))
    }
}

fn req_encode(req: &Json) -> Result<Json> {
    let (schema, limits) = req_schema(req)?;
    let msg_json = match req.get("message") {
        Some(m) => m.clone(),
        None => match req.get("message_file") {
            Some(Json::Str(p)) => json::parse(&fs::read_to_string(p)?)?,
            _ => {
                return Err(crate::Error::Json(
                    "encode request needs `message` object or `message_file`".into(),
                ))
            }
        },
    };
    let msg = value::json_to_message(&schema, &msg_json)?;
    let bin = writer::encode(&schema, &msg, &limits)?;
    let mut resp = maybe_write(req, &bin)?;
    if let Json::Object(o) = &mut resp {
        o.push((
            "fingerprint".into(),
            Json::Str(fingerprint::to_hex(fingerprint::fingerprint(&schema))),
        ));
    }
    Ok(resp)
}

fn req_decode(req: &Json) -> Result<Json> {
    let (schema, limits) = req_schema(req)?;
    let raw = req_input_bytes(req)?;
    let decoded = reader::decode(&schema, &raw, &limits)?;
    let message_json = value::message_to_json(&schema, &decoded.message);

    let mut pairs = vec![
        ("ok".to_string(), Json::Bool(true)),
        (
            "wire_fingerprint".to_string(),
            Json::Str(fingerprint::to_hex(decoded.wire_fingerprint)),
        ),
        (
            "schema_fingerprint".to_string(),
            Json::Str(fingerprint::to_hex(decoded.schema_fingerprint)),
        ),
        (
            "fingerprint_match".to_string(),
            Json::Bool(decoded.fingerprint_match()),
        ),
        ("message".to_string(), message_json),
    ];
    if let Some(Json::Str(p)) = req.get("output_file") {
        let text = json::to_string_pretty(
            &pairs
                .iter()
                .find(|(k, _)| k == "message")
                .map(|(_, v)| v.clone())
                .unwrap(),
        )?;
        fs::write(p, text)?;
        pairs.retain(|(k, _)| k != "message");
        pairs.push(("output_file".to_string(), Json::Str(p.clone())));
    }
    Ok(Json::Object(pairs))
}

fn req_forward(req: &Json) -> Result<Json> {
    let (schema, limits) = req_schema(req)?;
    let raw = req_input_bytes(req)?;
    let decoded = reader::decode(&schema, &raw, &limits)?;
    let bin = writer::encode(&schema, &decoded.message, &limits)?;
    maybe_write(req, &bin)
}

fn req_fingerprint(req: &Json) -> Result<Json> {
    let (schema, _) = req_schema(req)?;
    Ok(Json::Object(vec![
        ("ok".to_string(), Json::Bool(true)),
        (
            "fingerprint".to_string(),
            Json::Str(fingerprint::to_hex(fingerprint::fingerprint(&schema))),
        ),
    ]))
}
