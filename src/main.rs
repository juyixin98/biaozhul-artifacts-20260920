//! Command-line control entry point.
//!
//! Reads one JSON request (from a file argument, from stdin with `-`, or from
//! the first positional argument) and writes one JSON response.
//!
//! Examples:
//! ```text
//! echo '{"op":"encode","set":{"values":[1,2,3]}}' | rbitset -
//! rbitset request.json
//! rbitset '{"op":"decode","data":"...."}'
//! ```

use std::io::Read;
use std::process::ExitCode;

use rbitset::json::{dispatch, parse, stringify};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let input = match read_input(&args) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("rbitset: {e}");
            return ExitCode::from(2);
        }
    };

    let response = match parse(&input) {
        Ok(req) => dispatch(&req),
        Err(e) => error_response(&e.to_string()),
    };
    println!("{}", stringify(&response));

    // Business errors are reported inside a well-formed response; malformed
    // invocation/usage exits non-zero above. Operational protocol errors are
    // exit code 1 so scripts can distinguish them from a clean ok:true.
    let is_business_error = matches!(
        &response,
        rbitset::json::Json::Obj(map) if matches!(map.get("ok"), Some(rbitset::json::Json::Bool(false)))
    );
    if is_business_error {
        return ExitCode::from(1);
    }
    ExitCode::SUCCESS
}

fn error_response(message: &str) -> rbitset::json::Json {
    use rbitset::json::Json;
    let mut m = std::collections::BTreeMap::new();
    m.insert("ok".to_string(), Json::Bool(false));
    m.insert("error".to_string(), Json::Str(message.to_string()));
    Json::Obj(m)
}

fn read_input(args: &[String]) -> Result<String, String> {
    match args {
        [] => {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .map_err(|e| format!("failed to read stdin: {e}"))?;
            Ok(buf)
        }
        [path] if path == "-" => {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .map_err(|e| format!("failed to read stdin: {e}"))?;
            Ok(buf)
        }
        [path] => {
            // A literal request shorter than a plausible path is treated as
            // inline JSON; otherwise it names a file.
            if path.starts_with('{') {
                Ok(path.clone())
            } else {
                std::fs::read_to_string(path).map_err(|e| format!("failed to read {path}: {e}"))
            }
        }
        _ => Err("usage: rbitset [request.json | - | '{...json...}']".to_string()),
    }
}
