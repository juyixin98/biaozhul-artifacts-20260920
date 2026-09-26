//! Command-line front end for the JSON control entry.
//!
//! Usage:
//!
//! ```text
//! rbitmap [FILE]             # one JSON request; FILE or stdin
//! rbitmap --batch [FILE]    # newline-delimited requests, one response per line
//! rbitmap --help
//! ```
//!
//! Exit code is 0 when every request parses (an `"ok": false` response is
//! still a successful invocation — protocol errors are data, not crashes).

use std::io::{Read, Write};
use std::process::ExitCode;

use rbitmap::api;

fn read_all(path: Option<&str>) -> std::io::Result<String> {
    let mut s = String::new();
    match path {
        Some(p) => {
            std::fs::File::open(p)?.read_to_string(&mut s)?;
        }
        None => {
            std::io::stdin().read_to_string(&mut s)?;
        }
    }
    Ok(s)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut batch = false;
    let mut file: Option<String> = None;
    for a in &args {
        match a.as_str() {
            "--batch" => batch = true,
            "-h" | "--help" => {
                println!(
                    "rbitmap — hybrid u32 integer set codec\n\
                     \n\
                     USAGE:\n  \
                       rbitmap [FILE]          one JSON request (FILE or stdin)\n  \
                       rbitmap --batch [FILE]  newline-delimited requests\n  \
                       rbitmap --help\n\
                     \n\
                     See FORMAT.md for the RBM1 binary format and examples/ for requests."
                );
                return ExitCode::SUCCESS;
            }
            other if other.starts_with("--") => {
                eprintln!("unknown flag: {other}");
                return ExitCode::from(2);
            }
            other => {
                if file.is_some() {
                    eprintln!("multiple input files given");
                    return ExitCode::from(2);
                }
                file = Some(other.to_string());
            }
        }
    }

    let input = match read_all(file.as_deref()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to read input: {e}");
            return ExitCode::from(2);
        }
    };

    let stdout = std::io::stdout();
    let mut out = stdout.lock();

    if batch {
        for line in input.lines() {
            if line.trim().is_empty() {
                continue;
            }
            writeln!(out, "{}", api::handle(line)).ok();
        }
    } else {
        writeln!(out, "{}", api::handle(input.trim())).ok();
    }
    out.flush().ok();
    ExitCode::SUCCESS
}
