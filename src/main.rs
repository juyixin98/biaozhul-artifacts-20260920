//! `mvcc-server`: local HTTP verification entry point.
//!
//! Usage: `mvcc-server --dir ./data --addr 127.0.0.1:8080`

use std::path::PathBuf;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mut dir = PathBuf::from("./mvcc-data");
    let mut addr = String::from("127.0.0.1:8080");

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--dir" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    dir = PathBuf::from(v);
                }
            }
            "--addr" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    addr = v.clone();
                }
            }
            "-h" | "--help" => {
                println!("Usage: mvcc-server [--dir DIR] [--addr HOST:PORT]");
                println!("  --dir   data directory (default ./mvcc-data)");
                println!("  --addr  listen address  (default 127.0.0.1:8080)");
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("unknown argument: {other}");
                return ExitCode::FAILURE;
            }
        }
        i += 1;
    }

    let server = match mvcc_reclaim::http::Server::new(&dir) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to open store at {}: {e}", dir.display());
            return ExitCode::FAILURE;
        }
    };

    match server.serve(&addr) {
        Ok(bound) => {
            eprintln!("mvcc-server listening on http://{bound} (data: {})", dir.display());
        }
        Err(e) => {
            eprintln!("failed to bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    }

    // Park forever; Ctrl-C stops the process.
    loop {
        std::thread::sleep(std::time::Duration::from_secs(3600));
    }
}
