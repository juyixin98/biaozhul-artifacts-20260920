//! Tiny argument parser (avoids a full CLI crate dependency):
//! `--addr 127.0.0.1:8080 --data-dir ./data --max-body 67108864`.
//! Also supports `--flag=value` form.

use std::net::SocketAddr;
use std::path::PathBuf;

pub struct CliArgs {
    pub addr: SocketAddr,
    pub data_dir: PathBuf,
    pub max_body: usize,
}

impl CliArgs {
    pub fn parse() -> Self {
        let mut addr: SocketAddr = "0.0.0.0:8080".parse().unwrap();
        let mut data_dir = PathBuf::from("./data");
        let mut max_body: usize = 64 * 1024 * 1024;
        let mut args = std::env::args().skip(1);
        while let Some(a) = args.next() {
            let (flag, inline_val) = match a.split_once('=') {
                Some((k, v)) => (k.to_string(), Some(v.to_string())),
                None => (a, None),
            };
            // Flags without a value are handled before consuming another token.
            if matches!(flag.as_str(), "--help" | "-h") {
                println!(
                    "chunk-upload — immutable-object resumable chunk upload service\n\n\
                     USAGE:\n  chunk-upload [--addr 0.0.0.0:8080] [--data-dir ./data] \
                     [--max-body 67108864]\n"
                );
                std::process::exit(0);
            }
            let val = match inline_val {
                Some(v) => v,
                None => args.next().unwrap_or_else(|| panic!("missing value for {flag}")),
            };
            match flag.as_str() {
                "--addr" | "-a" => addr = val.parse().unwrap_or_else(|e| panic!("invalid addr: {e}")),
                "--data-dir" | "-d" => data_dir = PathBuf::from(val),
                "--max-body" => max_body = val.parse().unwrap_or_else(|e| panic!("invalid max-body: {e}")),
                other => panic!("unknown argument: {other}"),
            }
        }
        CliArgs {
            addr,
            data_dir,
            max_body,
        }
    }
}
