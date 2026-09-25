//! `http-framing-server` — local TCP reference service for the
//! http-framing crate.
//!
//! Usage:
//!
//! ```text
//! http-framing-server [--addr 127.0.0.1:8080] [--read-size 4096]
//!                     [--max-body-bytes N] [--max-chunk-size N]
//!                     [--max-request-line N] [--max-header-block N]
//!                     [--max-header-count N]
//! http-framing-server --stdio [--read-size N]   # read raw bytes from stdin
//! ```
//!
//! The server never proxies. Each framed request gets a 200 with a
//! JSON description; each framing failure gets one 4xx with an
//! `X-Frame-Error` header and the connection closes.

use std::io;
use std::process::ExitCode;

use http_framing::server::{handle, run, ServerConfig};
use http_framing::Limits;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut config = ServerConfig::default();
    let mut stdio = false;

    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        let value = |i: &usize, name: &str| -> String {
            args.get(i + 1)
                .cloned()
                .unwrap_or_else(|| fatal(&format!("option {name} requires a value")))
        };
        let num = |i: &usize, name: &str| -> u64 {
            value(i, name)
                .parse()
                .unwrap_or_else(|_| fatal(&format!("option {name} expects a non-negative integer")))
        };
        match arg.as_str() {
            "--addr" => {
                config.addr = value(&i, "--addr");
                i += 2;
            }
            "--read-size" => {
                config.read_size = num(&i, "--read-size") as usize;
                i += 2;
            }
            "--max-body-bytes" => {
                config.limits.max_body_bytes = num(&i, "--max-body-bytes");
                i += 2;
            }
            "--max-chunk-size" => {
                config.limits.max_chunk_size = num(&i, "--max-chunk-size");
                i += 2;
            }
            "--max-request-line" => {
                config.limits.max_request_line_bytes = num(&i, "--max-request-line") as usize;
                i += 2;
            }
            "--max-header-block" => {
                config.limits.max_header_block_bytes = num(&i, "--max-header-block") as usize;
                i += 2;
            }
            "--max-header-count" => {
                config.limits.max_header_count = num(&i, "--max-header-count") as usize;
                i += 2;
            }
            "--stdio" => {
                stdio = true;
                i += 1;
            }
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            other => fatal(&format!("unknown argument: {other}")),
        }
    }

    if stdio {
        let stdin = io::stdin();
        let stdout = io::stdout();
        if let Err(e) = handle(stdin.lock(), stdout.lock(), &config) {
            eprintln!("io error: {e}");
            return ExitCode::FAILURE;
        }
        ExitCode::SUCCESS
    } else {
        match run(config) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("failed to start server: {e}");
                ExitCode::FAILURE
            }
        }
    }
}

fn fatal(msg: &str) -> ! {
    eprintln!("error: {msg}");
    print_help();
    std::process::exit(2);
}

fn print_help() {
    eprintln!(
        "http-framing-server — local HTTP/1.1 request framing reference service\n\
         \n\
         USAGE:\n    http-framing-server [OPTIONS]\n\
         \n\
         OPTIONS:\n\
         \x20   --addr <HOST:PORT>          listen address (default 127.0.0.1:8080)\n\
         \x20   --stdio                    frame bytes from stdin, write responses to stdout\n\
         \x20   --read-size <N>            bytes per read() (default 4096; try 1)\n\
         \x20   --max-request-line <N>     request line byte cap (default 8192)\n\
         \x20   --max-header-block <N>     header block byte cap (default 65536)\n\
         \x20   --max-header-count <N>     max header fields (default 100)\n\
         \x20   --max-body-bytes <N>       total body cap (default 1048576)\n\
         \x20   --max-chunk-size <N>       per-chunk cap (default 1048576)\n\
         \x20   -h, --help                 show this help\n"
    );
    let _ = Limits::default();
}
