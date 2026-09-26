//! JSON control entry point.
//!
//! Reads exactly one JSON request object from stdin (or from the file named
//! by `--request PATH`), executes it, and prints one JSON response object to
//! stdout. Exit code is 0 when the response has `"ok": true`, 1 otherwise.

use std::io::Read;

/// Requests larger than this are rejected; the schema is small and fixed.
const MAX_REQUEST_BYTES: u64 = 1 << 20; // 1 MiB

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let input = match read_request(&args) {
        Ok(s) => s,
        Err(e) => {
            println!(r#"{{"ok":false,"error":"{e}"}}"#);
            std::process::exit(1);
        }
    };

    let response = ecstripe::control::run_json(&input);
    println!("{response}");

    let ok = serde_json::from_str::<serde_json::Value>(&response)
        .ok()
        .and_then(|v| v.get("ok").and_then(|b| b.as_bool()))
        .unwrap_or(false);
    std::process::exit(if ok { 0 } else { 1 });
}

fn read_request(args: &[String]) -> Result<String, String> {
    match args.get(1).map(String::as_str) {
        None => {
            let mut buf = String::new();
            std::io::stdin()
                .take(MAX_REQUEST_BYTES)
                .read_to_string(&mut buf)
                .map_err(|e| format!("failed to read stdin: {e}"))?;
            Ok(buf)
        }
        Some("--request") => {
            let path = args
                .get(2)
                .ok_or_else(|| "--request requires a path".to_string())?;
            let meta = std::fs::metadata(path)
                .map_err(|e| format!("cannot stat {path}: {e}"))?;
            if meta.len() > MAX_REQUEST_BYTES {
                return Err(format!("request file exceeds {MAX_REQUEST_BYTES} bytes"));
            }
            std::fs::read_to_string(path).map_err(|e| format!("cannot read {path}: {e}"))
        }
        Some(other) => Err(format!(
            "unknown argument {other:?}; usage: ecstripe [--request PATH] < request.json"
        )),
    }
}
