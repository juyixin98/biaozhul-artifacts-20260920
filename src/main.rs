//! `imerkle` 命令行入口。
//!
//! 用法：
//! ```text
//! imerkle serve --addr 127.0.0.1:8080 --data-dir ./data
//! ```

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("serve") => serve(&args[2..]),
        Some("-h") | Some("--help") | Some("help") | None => {
            print_help();
            ExitCode::SUCCESS
        }
        Some(other) => {
            eprintln!("unknown command: {other}");
            print_help();
            ExitCode::FAILURE
        }
    }
}

fn print_help() {
    eprintln!(
        "incremental-merkle (imerkle)\n\
\n\
USAGE:\n\
  imerkle serve [--addr 127.0.0.1:8080] [--data-dir ./data]\n\
\n\
Routes:\n\
  POST /repos/:name/open?chunk_size=4096\n\
  GET  /repos/:name\n\
  PUT  /repos/:name/file                 body: {{\"data\":\"<hex>\"}} 或裸 hex\n\
  GET  /repos/:name/proof?start=&end=\n\
  POST /verify                           body: RangeProof JSON\n"
    );
}

fn serve(args: &[String]) -> ExitCode {
    let mut addr = "127.0.0.1:8080".to_string();
    let mut data_dir = "./data".to_string();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--addr" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    addr = v.clone();
                }
            }
            "--data-dir" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    data_dir = v.clone();
                }
            }
            other => {
                eprintln!("unknown option: {other}");
                return ExitCode::FAILURE;
            }
        }
        i += 1;
    }
    let server = incremental_merkle::http::Server::new(&data_dir);
    match server.serve(&addr) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("server error: {e}");
            ExitCode::FAILURE
        }
    }
}
