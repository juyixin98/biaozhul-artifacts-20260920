//! Command-line entry point for the `cdict` JSON control plane.
//!
//! Usage:
//!
//! ```text
//! cdict <request.json>                 # run one JSON request
//! cdict -                              # read JSON request from stdin
//! cdict --help | --version
//! ```
//!
//! The request is a single JSON object (see `src/control.rs`). Operation data
//! files are referenced inside the request. When the request's `output` is a
//! file, the JSON response is printed to stdout. When `output` is `"-"` the
//! data is emitted to stdout and the response is printed to stderr, so
//! `cdict - < req.json > out.cdc` keeps the stream and the status report
//! apart.

use cdict::control::execute;
use cdict::error::Error;
use cdict::json::{self, Json};
use std::io::{Read, Write};
use std::process::ExitCode;

const HELP: &str = "\
cdict — streaming columnar dictionary encoding codec

USAGE:
    cdict <request.json>      Execute the JSON control request in the file
    cdict -                   Read the JSON control request from stdin
    cdict --help              Show this help
    cdict --version           Show version

REQUEST (single JSON object):
    {
      \"op\": \"encode\" | \"decode\" | \"merge\" | \"inspect\",
      \"input\":  \"path or -\",        // encode/decode/inspect (stdin)
      \"inputs\": [\"p1\", \"p2\"],      // merge
      \"output\": \"path or -\",        // encode/decode/merge (stdout)
      \"segment_rows\": 100000,         // optional; 0 = single segment
      \"limits\": {                      // optional overrides
        \"max_dict_bytes\": 268435456,
        \"max_dict_entries\": 10000000,
        \"max_rows\": 10000000,
        \"max_string_bytes\": 16777216,
        \"max_decoded_string_bytes\": 536870912,
        \"max_segments\": 10000
      }
    }

ENCODE input / DECODE output row format:
    JSON array of string or null, e.g. [\"a\", null, \"b\", \"a\"]

RESPONSE:
    {\"ok\": true, \"op\": \"...\", ...} on success
    {\"ok\": false, \"error\": \"...\"}   on failure (exit code 1)
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        print!("{HELP}");
        return ExitCode::SUCCESS;
    }
    if args.iter().any(|a| a == "--version" || a == "-V") {
        println!("cdict {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }

    match run(&args) {
        Ok((response, data_on_stdout)) => {
            let line = json::to_string(&response);
            if data_on_stdout {
                eprintln!("{line}");
            } else {
                println!("{line}");
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            let response = Json::Object(vec![
                ("ok".to_string(), Json::Bool(false)),
                ("error".to_string(), Json::String(e.to_string())),
            ]);
            eprintln!("{}", json::to_string(&response));
            ExitCode::from(1)
        }
    }
}

/// Returns the response and whether the operation's data payload was sent to
/// stdout (in which case the response must go to stderr).
fn run(args: &[String]) -> Result<(Json, bool), Error> {
    if args.len() != 1 {
        return Err(Error::Invalid(
            "expected exactly one argument: path to request JSON, or '-' for stdin".into(),
        ));
    }
    let bytes = if args[0] == "-" {
        let mut buf = Vec::new();
        std::io::stdin().read_to_end(&mut buf).map_err(Error::io)?;
        buf
    } else {
        std::fs::read(&args[0])
            .map_err(|e| Error::Io(format!("cannot read request file {:?}: {}", args[0], e)))?
    };
    let req = Json::parse(&bytes)?;
    let data_on_stdout = matches!(req.get("output").and_then(Json::as_str), Some("-"));

    let mut stdout = std::io::stdout();
    let response = execute(&req, &mut stdout)?;
    stdout.flush().map_err(Error::io)?;
    Ok((response, data_on_stdout))
}
