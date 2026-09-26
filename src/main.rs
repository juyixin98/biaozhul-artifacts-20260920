//! CLI front end: reads one JSON request from a file argument or stdin and
//! writes the JSON response to stdout. Exit code 0 on success, 1 on error.

use std::io::Read;
use std::process::ExitCode;

/// Maximum accepted request size (a request may embed a base64 payload).
const MAX_INPUT_BYTES: usize = 512 * 1024 * 1024;

fn main() -> ExitCode {
    let arg = std::env::args().nth(1);
    let input = match &arg {
        Some(path) => match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("cannot read {path}: {e}");
                return ExitCode::from(2);
            }
        },
        None => {
            let mut s = String::new();
            if let Err(e) = std::io::stdin().read_to_string(&mut s) {
                eprintln!("cannot read stdin: {e}");
                return ExitCode::from(2);
            }
            s
        }
    };
    if input.len() > MAX_INPUT_BYTES {
        println!("{{\"ok\":false,\"error\":\"request too large\"}}");
        return ExitCode::FAILURE;
    }
    let (response, ok) = bpb::json_io::handle_request(&input);
    println!("{response}");
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
