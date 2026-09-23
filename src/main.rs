//! `mvcc-gc` server binary.
//!
//! Usage: mvcc-gc [--addr 127.0.0.1:8080] [--data ./data]

use std::process::ExitCode;
use std::sync::Arc;

use mvcc_gc::engine::Engine;
use mvcc_gc::server::Server;
use mvcc_gc::vfs::RealVfs;

fn main() -> ExitCode {
    let mut addr = "127.0.0.1:8080".to_string();
    let mut data = "./data".to_string();

    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--addr" => match args.next() {
                Some(v) => addr = v,
                None => return usage(),
            },
            "--data" => match args.next() {
                Some(v) => data = v,
                None => return usage(),
            },
            "--help" | "-h" => return usage_ok(),
            other => {
                eprintln!("unknown argument: {other}");
                return usage();
            }
        }
    }

    let engine = match Engine::open(&data, Arc::new(RealVfs::new())) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("failed to open store at {data}: {e}");
            return ExitCode::FAILURE;
        }
    };
    eprintln!(
        "mvcc-gc: data dir {data}, current version {}",
        engine.current_version()
    );

    let server = Server::new(engine, addr);
    match server.serve() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("server error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn usage() -> ExitCode {
    eprintln!("usage: mvcc-gc [--addr 127.0.0.1:8080] [--data ./data]");
    ExitCode::FAILURE
}

fn usage_ok() -> ExitCode {
    println!("usage: mvcc-gc [--addr 127.0.0.1:8080] [--data ./data]");
    ExitCode::SUCCESS
}
