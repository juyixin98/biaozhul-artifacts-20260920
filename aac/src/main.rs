//! `aac` command-line entry point.
//!
//! Modes:
//!
//! * `aac encode -i SRC -o OUT.ac01 [--max-payload-bytes N]`
//! * `aac decode -i SRC.ac01 -o OUT   [--max-output-bytes N]`
//! * `aac json [REQ.json]` — JSON control entry; request on stdin (or from a
//!   file argument), response on stdout. Schema:
//!
//! ```json
//! { "op": "encode", "data": "<base64 raw bytes>", "max_output": 12345 }
//! { "op": "decode", "data": "<base64 AC01 container>", "max_output": 12345 }
//! ```
//!
//! `max_output` is optional; encode applies it to coded payload size, decode
//! to decoded byte length. Responses:
//!
//! ```json
//! { "ok": true, "op": "encode", "result": "<base64>",
//!   "stats": { "input_bytes": 0, "output_bytes": 0,
//!              "coded_bits": 0, "rescales": 0, "symbols": 0 } }
//! { "ok": false, "error": "UnexpectedEnd", "message": "..." }
//! ```

use aac::jsonutil::{base64_decode, base64_encode, escape_string, parse, Json};
use aac::{pack_stream, unpack, unpack_stream};
use std::fs::File;
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::process::ExitCode;

fn print_usage() {
    eprintln!(
        "aac — adaptive arithmetic coding\n\
\n\
USAGE:\n\
  aac encode -i SRC -o OUT.ac01 [--max-payload-bytes N]\n\
  aac decode -i SRC.ac01 -o OUT     [--max-output-bytes N]\n\
  aac json [REQUEST.json]           # request from stdin if no file given\n"
    );
}

fn open_input(path: &str) -> io::Result<Box<dyn Read>> {
    if path == "-" {
        Ok(Box::new(BufReader::new(io::stdin())))
    } else {
        Ok(Box::new(BufReader::new(File::open(path)?)))
    }
}

fn open_output(path: &str) -> io::Result<Box<dyn Write>> {
    if path == "-" {
        Ok(Box::new(BufWriter::new(io::stdout())))
    } else {
        Ok(Box::new(BufWriter::new(File::create(path)?)))
    }
}

/// Parse `--flag value`, `--flag=value`, `-f value` and `-f=value` args.
/// `long` is the double-dash name, `short` the optional single-dash name.
struct Args<'a> {
    items: Vec<&'a str>,
}

impl<'a> Args<'a> {
    fn lookup(&self, long: &str, short: Option<char>) -> Option<&'a str> {
        let eq_long = format!("--{long}=");
        let dash_long = format!("--{long}");
        let mut i = 0;
        while i < self.items.len() {
            let it = self.items[i];
            if let Some(rest) = it.strip_prefix(&eq_long) {
                return Some(rest);
            }
            if it == dash_long && i + 1 < self.items.len() {
                return Some(self.items[i + 1]);
            }
            if let Some(s) = short {
                let eq_short = format!("-{s}=");
                let dash_short = format!("-{s}");
                if let Some(rest) = it.strip_prefix(&eq_short) {
                    return Some(rest);
                }
                if it == dash_short && i + 1 < self.items.len() {
                    return Some(self.items[i + 1]);
                }
            }
            i += 1;
        }
        None
    }

    /// Value for a `--long` option with a `-s` short alias.
    fn opt(&self, long: &str, short: char) -> Option<&'a str> {
        self.lookup(long, Some(short))
    }
}

fn run_file_encode(args: &Args) -> ExitCode {
    let (Some(src), Some(dst)) = (args.opt("input", 'i'), args.opt("output", 'o')) else {
        eprintln!("error: encode requires --input and --output");
        print_usage();
        return ExitCode::from(2);
    };
    let cap = match parse_optional_u64(args, "max-payload-bytes") {
        Ok(v) => v,
        Err(m) => {
            eprintln!("error: {m}");
            return ExitCode::from(2);
        }
    };
    let result = (|| -> io::Result<()> {
        let input = open_input(src)?;
        let output = open_output(dst)?;
        let stats = pack_stream(input, output, cap)?;
        eprintln!(
            "encoded {src} -> {dst}: {} -> {} bytes ({} bits, {} rescales, {} symbols)",
            stats.input_bytes, stats.output_bytes, stats.coded_bits, stats.rescales, stats.symbols
        );
        Ok(())
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run_file_decode(args: &Args) -> ExitCode {
    let (Some(src), Some(dst)) = (args.opt("input", 'i'), args.opt("output", 'o')) else {
        eprintln!("error: decode requires --input and --output");
        print_usage();
        return ExitCode::from(2);
    };
    let cap = match parse_optional_u64(args, "max-output-bytes") {
        Ok(v) => v.unwrap_or(aac::DEFAULT_MAX_OUTPUT),
        Err(m) => {
            eprintln!("error: {m}");
            return ExitCode::from(2);
        }
    };
    let result = (|| -> io::Result<()> {
        let input = open_input(src)?;
        let mut output = open_output(dst)?;
        let stats = unpack_stream(input, &mut output, Some(cap))?;
        output.flush()?;
        eprintln!(
            "decoded {src} -> {dst}: {} coded input bytes -> {} bytes ({} rescales)",
            stats.input_bytes, stats.output_bytes, stats.rescales
        );
        Ok(())
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn parse_optional_u64(args: &Args, name: &str) -> Result<Option<u64>, String> {
    match args.lookup(name, None) {
        Some(v) => v
            .parse::<u64>()
            .map(Some)
            .map_err(|_| format!("bad --{name}: {v}")),
        None => Ok(None),
    }
}

// ---------------------------------------------------------------------------
// JSON control entry
// ---------------------------------------------------------------------------

fn stats_json(s: &aac::Stats) -> String {
    format!(
        "{{\"input_bytes\":{},\"output_bytes\":{},\"coded_bits\":{},\"rescales\":{},\"symbols\":{}}}",
        s.input_bytes, s.output_bytes, s.coded_bits, s.rescales, s.symbols
    )
}

fn error_response(op: &str, kind: &str, message: &str) -> String {
    format!(
        "{{\"ok\":false,\"op\":{},\"error\":{},\"message\":{}}}",
        escape_string(op),
        escape_string(kind),
        escape_string(message)
    )
}

fn handle_json_request(text: &str) -> String {
    let req = match parse(text) {
        Ok(v) => v,
        Err(e) => return error_response("", e.kind(), &e.to_string()),
    };
    let op = match req.get("op").and_then(Json::as_str) {
        Some(op) => op.to_string(),
        None => {
            return error_response(
                "",
                "InvalidJson",
                "missing or invalid string field \"op\" (\"encode\"|\"decode\")",
            )
        }
    };
    let data_b64 = match req.get("data").and_then(Json::as_str) {
        Some(d) => d,
        None => {
            return error_response(&op, "InvalidJson", "missing string field \"data\" (base64)");
        }
    };
    let max_output = match req.get("max_output") {
        None => None,
        Some(Json::Int(n)) if *n >= 0 => Some(*n as u64),
        Some(_) => {
            return error_response(
                &op,
                "InvalidJson",
                "\"max_output\" must be a non-negative integer",
            );
        }
    };

    let respond = |result: aac::Result<(Vec<u8>, aac::Stats)>| -> String {
        match result {
            Ok((bytes, stats)) => format!(
                "{{\"ok\":true,\"op\":{},\"result\":{},\"stats\":{}}}",
                escape_string(&op),
                escape_string(&base64_encode(&bytes)),
                stats_json(&stats)
            ),
            Err(e) => error_response(&op, e.kind(), &e.to_string()),
        }
    };

    let raw = match base64_decode(data_b64) {
        Ok(v) => v,
        Err(e) => return error_response(&op, e.kind(), &e.to_string()),
    };

    match op.as_str() {
        // encode: raw source bytes -> AC01 container
        "encode" => respond(aac::pack(&raw, max_output)),
        // decode: AC01 container -> raw source bytes
        "decode" => respond(unpack(&raw, max_output)),
        other => error_response(other, "InvalidJson", "op must be \"encode\" or \"decode\""),
    }
}

fn run_json(path: Option<&str>) -> ExitCode {
    let text = match path {
        Some(p) => match std::fs::read_to_string(p) {
            Ok(t) => t,
            Err(e) => {
                println!(
                    "{}",
                    error_response("", "Io", &format!("cannot read {p}: {e}"))
                );
                return ExitCode::FAILURE;
            }
        },
        None => {
            let mut buf = String::new();
            if let Err(e) = io::stdin().read_to_string(&mut buf) {
                println!(
                    "{}",
                    error_response("", "Io", &format!("cannot read stdin: {e}"))
                );
                return ExitCode::FAILURE;
            }
            buf
        }
    };
    let response = handle_json_request(&text);
    println!("{response}");
    // Exit non-zero on a failed op for scriptability, while still emitting the
    // JSON error envelope.
    if response.contains("\"ok\":false") {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    if raw.is_empty() {
        print_usage();
        return ExitCode::from(2);
    }
    let cmd = raw[0].as_str();
    let args = Args {
        items: raw.iter().skip(1).map(String::as_str).collect(),
    };
    match cmd {
        "encode" => run_file_encode(&args),
        "decode" => run_file_decode(&args),
        "json" => run_json(args.items.first().copied()),
        "-h" | "--help" | "help" => {
            print_usage();
            ExitCode::SUCCESS
        }
        other => {
            eprintln!("error: unknown subcommand '{other}'");
            print_usage();
            ExitCode::from(2)
        }
    }
}
