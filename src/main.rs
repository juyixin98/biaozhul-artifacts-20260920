//! cas-store binary: `cas-store [PATH] [--addr ADDR]`.
//!
//! Defaults: repository at `./cas-data`, listening on `127.0.0.1:8080`.

use std::path::PathBuf;
use std::sync::Arc;

use cas_store::{RealFs, Repository};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut path = PathBuf::from("cas-data");
    let mut addr = "127.0.0.1:8080".to_string();
    let mut iter = args.iter();
    while let Some(a) = iter.next() {
        match a.as_str() {
            "--addr" | "-a" => {
                if let Some(v) = iter.next() {
                    addr = v.clone();
                }
            }
            "--help" | "-h" => {
                println!("usage: cas-store [REPO_DIR] [--addr HOST:PORT]");
                println!("  defaults: REPO_DIR=./cas-data  ADDR=127.0.0.1:8080");
                std::process::exit(0);
            }
            other if !other.starts_with('-') => path = PathBuf::from(other),
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }

    match Repository::open(&path, Arc::new(RealFs::new())) {
        Ok(repo) => {
            if let Err(e) = cas_store::server::serve(repo, &addr) {
                eprintln!("server error: {e}");
                std::process::exit(1);
            }
        }
        Err(e) => {
            eprintln!("failed to open repository at {}: {e}", path.display());
            std::process::exit(1);
        }
    }
}
