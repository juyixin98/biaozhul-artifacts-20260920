//! tsblock-server: local HTTP validation entry for the time-series block store.
//!
//! Usage:
//!   tsblock-server [--dir DATA_DIR] [--addr 127.0.0.1:8080]
//!                  [--buffer-late] [--block-size N]

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tsblock::http;
use tsblock::io::FsIO;
use tsblock::storage::{LateMode, Repo, DEFAULT_BLOCK_SIZE};

fn main() {
    let mut dir = PathBuf::from("tsblock-data");
    let mut addr = "127.0.0.1:8080".to_string();
    let mut mode = LateMode::Reject;
    let mut block_size = DEFAULT_BLOCK_SIZE;

    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--dir" => {
                i += 1;
                dir = PathBuf::from(args.get(i).expect("--dir needs a value"));
            }
            "--addr" => {
                i += 1;
                addr = args.get(i).expect("--addr needs a value").clone();
            }
            "--buffer-late" => mode = LateMode::Buffer,
            "--block-size" => {
                i += 1;
                block_size = args
                    .get(i)
                    .expect("--block-size needs a value")
                    .parse()
                    .expect("--block-size must be a positive integer");
            }
            "--help" | "-h" => {
                println!(
                    "tsblock-server [--dir DATA_DIR] [--addr 127.0.0.1:8080] [--buffer-late] [--block-size N]"
                );
                return;
            }
            other => {
                eprintln!("unknown argument: {}", other);
                std::process::exit(2);
            }
        }
        i += 1;
    }

    std::fs::create_dir_all(&dir).expect("create data dir");
    let io = FsIO::new(&dir);
    let repo = Repo::open(io, mode, block_size).expect("open repository");
    eprintln!(
        "tsblock-server: dir={:?} addr={} late_mode={:?} block_size={}",
        dir, addr, mode, block_size
    );
    http::serve(Arc::new(Mutex::new(repo)), &addr).expect("serve");
}
