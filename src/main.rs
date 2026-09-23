//! `psst` command line: runs the local HTTP verification server.
//!
//! Usage:
//!
//! ```text
//! psst serve --dir ./tabledata --addr 127.0.0.1:8088
//! ```

use std::path::PathBuf;
use std::process::ExitCode;

use psst::server::HttpServer;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("psst: {msg}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let mut dir = PathBuf::from("./tabledata");
    let mut addr = String::from("127.0.0.1:8088");

    let mut iter = args.iter().skip(1);
    let cmd = iter.next().ok_or_else(usage)?;
    if cmd != "serve" {
        return Err(usage());
    }
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--dir" | "-d" => {
                dir = PathBuf::from(iter.next().ok_or("--dir needs a value")?);
            }
            "--addr" | "-a" => {
                addr = iter.next().ok_or("--addr needs a value")?.clone();
            }
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }

    let server = HttpServer::bind(dir, &addr).map_err(|e| e.to_string())?;
    println!(
        "psst serving {} on http://{}",
        server.local_addr().map_err(|e| e.to_string())?,
        addr
    );
    server.serve().map_err(|e| e.to_string())
}

const USAGE: &str = "\
psst — prefix-compressed sorted table, local HTTP verification server

USAGE:
    psst serve [--dir DIR] [--addr HOST:PORT]

OPTIONS:
    -d, --dir <DIR>      directory for table files (default ./tabledata)
    -a, --addr <ADDR>   listen address (default 127.0.0.1:8088)
    -h, --help           show this help

ENDPOINTS (keys/values are hex strings):
    GET    /healthz
    GET    /tables
    PUT    /tables/{name}            body: {\"entries\":[[key,value],...], ...options}
    DELETE /tables/{name}
    GET    /tables/{name}/get?key=<hex>
    GET    /tables/{name}/scan?start=<hex>&end=<hex>&end_inclusive&limit=N
    POST   /tables/{name}/validate
";

fn usage() -> String {
    "missing or unknown command\n".to_string() + USAGE
}
