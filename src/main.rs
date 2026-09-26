//! Command-line JSON control entry point for the `bse` codec.
//!
//! Usage:
//!
//! ```text
//! bse [--pretty] [-f request.json | -]
//! ```
//!
//! With no `-f`, the JSON request is read from standard input. The JSON
//! response is written to standard output and the process exits non-zero
//! (with an `{"ok": false, ...}` body) when the request fails.

use std::io::Read;
use std::process::ExitCode;

use bse::api;
use bse::json::{parse, to_string, to_string_pretty};

fn print_help() {
    println!(
        "bse — streaming binary schema-evolution codec\n\n\
USAGE:\n    bse [--pretty] [-f <request.json> | -]\n\n\
The request is a JSON object with an \"op\" field:\n\
  encode    schema + message JSON   -> BSE1 bytes (base64/hex)\n\
  decode    BSE1 bytes + schema     -> message JSON + stats\n\
  forward   decode, patch, re-encode while preserving unknown fields\n\
  compat    compare an old and a new schema\n\
  truncate  cut an envelope's payload to N bytes (for truncation tests)\n\n\
With no -f the request is read from stdin. See README.md for examples."
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut pretty = false;
    let mut file: Option<String> = None;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--pretty" => pretty = true,
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            "-f" => {
                i += 1;
                if i >= args.len() {
                    eprintln!("error: -f requires a path (use - for stdin)");
                    return ExitCode::from(2);
                }
                file = Some(args[i].clone());
            }
            other if other.starts_with("--file=") => {
                file = Some(other["--file=".len()..].to_string());
            }
            "-" => file = Some("-".to_string()),
            other => {
                eprintln!("error: unknown argument '{other}' (try --help)");
                return ExitCode::from(2);
            }
        }
        i += 1;
    }

    let input = match read_input(file.as_deref()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("error: {e}");
            return ExitCode::from(2);
        }
    };

    let request = match parse(&input) {
        Ok(v) => v,
        Err(e) => return fail(&e, pretty),
    };

    match api::execute(&request) {
        Ok(response) => {
            let text = if pretty {
                to_string_pretty(&response).unwrap_or_else(|e| e.to_string())
            } else {
                to_string(&response).unwrap_or_else(|e| e.to_string())
            };
            println!("{text}");
            ExitCode::SUCCESS
        }
        Err(e) => fail(&e, pretty),
    }
}

fn fail(e: &bse::Error, pretty: bool) -> ExitCode {
    let response = api::error_response(e);
    let text = if pretty {
        to_string_pretty(&response).unwrap_or_else(|e| e.to_string())
    } else {
        to_string(&response).unwrap_or_else(|e| e.to_string())
    };
    println!("{text}");
    let _ = pretty;
    ExitCode::from(1)
}

fn read_input(file: Option<&str>) -> std::io::Result<String> {
    match file {
        None | Some("-") => {
            let mut s = String::new();
            std::io::stdin().read_to_string(&mut s)?;
            Ok(s)
        }
        Some(path) => std::fs::read_to_string(path),
    }
}
