//! `ifix` command line — a thin front-end over the JSON control entry point.
//!
//! Modes:
//!   ifix build  --output FILE --request REQUEST.json [--base DIR]
//!   ifix verify --index FILE [--backend read|mmap]
//!   ifix lookup --index FILE --path /a/b [--backend read|mmap]
//!   ifix list   --index FILE --path /a   [--backend read|mmap]
//!   ifix read   --index FILE --path /a/b [--max-inline-base64 N]
//!   ifix stats  --index FILE
//!   ifix json [--base DIR]              (read one JSON request from stdin)
//!
//! Exit code 0 on `"status":"ok"`, 1 on error. All output is JSON.

use ifix::control;
use ifix::error::IfixError;
use std::io::{BufReader, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(value) => {
            let stdout = std::io::stdout();
            let mut lock = stdout.lock();
            let _ = serde_json::to_writer_pretty(&mut lock, &value);
            let _ = writeln!(lock);
            let ok = value.get("status").and_then(|s| s.as_str()) == Some("ok");
            if ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(1)
            }
        }
        Err(err) => {
            let envelope = control::envelope(Err::<serde_json::Value, _>(err));
            let stdout = std::io::stdout();
            let mut lock = stdout.lock();
            let _ = serde_json::to_writer_pretty(&mut lock, &envelope);
            let _ = writeln!(lock);
            ExitCode::from(1)
        }
    }
}

fn run(args: &[String]) -> Result<serde_json::Value, IfixError> {
    // First positional word is the command; everything else is --flag value.
    let (command, flag_args) = match args.first() {
        Some(first) if !first.starts_with('-') => (first.clone(), &args[1..]),
        _ => ("json".to_owned(), args),
    };
    if command == "help" || args.iter().any(|a| a == "--help" || a == "-h") {
        print_help();
        std::process::exit(0);
    }

    let mut map = std::collections::BTreeMap::new();
    let mut i = 0;
    while i < flag_args.len() {
        let a = &flag_args[i];
        let key = a
            .strip_prefix("--")
            .ok_or_else(|| IfixError::Json(format!("expected --flag, got {a:?}")))?;
        let value = flag_args
            .get(i + 1)
            .ok_or_else(|| IfixError::Json(format!("flag --{key} requires a value")))?;
        map.insert(key.to_owned(), value.clone());
        i += 2;
    }

    let cwd = std::env::current_dir().map_err(IfixError::from)?;
    let base = map
        .remove("base")
        .map(std::path::PathBuf::from)
        .unwrap_or(cwd);

    if command == "json" {
        let limits = ifix::format::Limits::default();
        let mut stdin = BufReader::new(std::io::stdin());
        let request = control::read_request(&mut stdin, &limits)?;
        return Ok(control::envelope(control::dispatch(&request, &base)));
    }

    let mut request = serde_json::Map::new();
    request.insert("command".into(), serde_json::Value::String(command.clone()));

    // For `build`, --request points at a JSON file containing the tree. Its
    // fields provide defaults; explicit CLI flags below override them.
    if command == "build" {
        if let Some(path) = map.remove("request") {
            let text = std::fs::read_to_string(&path)
                .map_err(|e| IfixError::Json(format!("cannot read request {path}: {e}")))?;
            let extra: serde_json::Value = serde_json::from_str(&text)?;
            if let Some(obj) = extra.as_object() {
                for (k, v) in obj {
                    request.insert(k.clone(), v.clone());
                }
            }
        }
    }

    // CLI flags win over request-file fields. Keys use hyphens on the
    // command line but underscores in the JSON control surface. Numeric
    // flags arrive as strings and must be inserted as JSON numbers, since
    // the control layer reads them with `as_u64()`.
    const NUMERIC_FLAGS: &[&str] = &["log2_page", "max_inline_base64"];
    for (k, v) in map {
        let key = k.replace('-', "_");
        let value = if NUMERIC_FLAGS.contains(&key.as_str()) {
            match v.parse::<u64>() {
                Ok(n) => serde_json::Value::from(n),
                Err(_) => {
                    return Err(IfixError::Json(format!(
                        "flag --{k} expects a non-negative integer, got {v:?}"
                    )));
                }
            }
        } else {
            serde_json::Value::String(v)
        };
        request.insert(key, value);
    }

    Ok(control::envelope(control::dispatch(
        &serde_json::Value::Object(request),
        &base,
    )))
}

fn print_help() {
    println!(
        "ifix — immutable file index\n\n\
USAGE:\n  \
ifix <command> [--flag value ...]\n  \
ifix json < request.json\n\n\
COMMANDS:\n  \
build   --output FILE --request tree.json [--base DIR]\n  \
verify  --index FILE [--backend read|mmap]\n  \
lookup  --index FILE --path /a/b [--backend read|mmap]\n  \
list    --index FILE --path /a [--backend read|mmap]\n  \
read    --index FILE --path /a/b [--max-inline-base64 N]\n  \
stats   --index FILE\n\n\
All output is JSON; exit code 0 on success, 1 on error."
    );
}
