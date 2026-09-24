//! HTTP server entry point.
//!
//! Usage:
//!   ext-hash-index --file ./data.idx --addr 127.0.0.1:3000 \
//!       [--capacity 8] [--max-depth 20] [--hash fnv1a64|constant|u64lowbits]
//!
//! If `--file` does not exist it is created with the given parameters;
//! otherwise it is opened and the creation flags are ignored (the hash
//! function and limits are stored in the file header). A crash-interrupted
//! structural operation is replayed automatically on open.
use axum::serve;
use ext_hash_index::server::router;
use ext_hash_index::{Config, HashKind, Index};
use std::net::SocketAddr;
use std::path::PathBuf;
use tokio::net::TcpListener;

struct Args {
    file: PathBuf,
    addr: SocketAddr,
    capacity: usize,
    max_depth: u32,
    hash: HashKind,
    key_max: usize,
    val_max: usize,
}

fn usage() -> ! {
    eprintln!(
        "usage: ext-hash-index --file <PATH> [--addr 127.0.0.1:3000] \
         [--capacity 8] [--max-depth 20] [--key-max 128] [--val-max 256] \
         [--hash fnv1a64|constant|u64lowbits]"
    );
    std::process::exit(2);
}

fn parse_args() -> Args {
    let mut args = Args {
        file: PathBuf::from("index.db"),
        addr: "127.0.0.1:3000".parse().unwrap(),
        capacity: 8,
        max_depth: 20,
        hash: HashKind::Fnv1a64,
        key_max: 128,
        val_max: 256,
    };
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let val = |name: &str, it: &mut dyn Iterator<Item = String>| -> String {
            it.next().unwrap_or_else(|| {
                eprintln!("flag {} requires a value", name);
                usage();
            })
        };
        match flag.as_str() {
            "--file" => args.file = PathBuf::from(val("--file", &mut it)),
            "--addr" => {
                let v = val("--addr", &mut it);
                args.addr = v.parse().unwrap_or_else(|_| {
                    eprintln!("bad --addr: {}", v);
                    usage();
                });
            }
            "--capacity" => {
                let v = val("--capacity", &mut it);
                args.capacity = v.parse().unwrap_or_else(|_| {
                    eprintln!("bad --capacity: {}", v);
                    usage();
                });
            }
            "--max-depth" => {
                let v = val("--max-depth", &mut it);
                args.max_depth = v.parse().unwrap_or_else(|_| {
                    eprintln!("bad --max-depth: {}", v);
                    usage();
                });
            }
            "--key-max" => {
                let v = val("--key-max", &mut it);
                args.key_max = v.parse().unwrap_or_else(|_| {
                    eprintln!("bad --key-max: {}", v);
                    usage();
                });
            }
            "--val-max" => {
                let v = val("--val-max", &mut it);
                args.val_max = v.parse().unwrap_or_else(|_| {
                    eprintln!("bad --val-max: {}", v);
                    usage();
                });
            }
            "--hash" => {
                let v = val("--hash", &mut it);
                args.hash = HashKind::parse(&v).unwrap_or_else(|| {
                    eprintln!(
                        "unknown --hash '{}' (want fnv1a64, constant or u64lowbits)",
                        v
                    );
                    usage();
                });
            }
            "-h" | "--help" => usage(),
            other => {
                eprintln!("unknown argument: {}", other);
                usage();
            }
        }
    }
    args
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args();

    let exists = args.file.exists();
    let index = if exists {
        eprintln!(
            "[ext-hash-index] opening existing index {}",
            args.file.display()
        );
        Index::open(&args.file)?
    } else {
        eprintln!(
            "[ext-hash-index] creating index {} (capacity={}, max_depth={}, hash={})",
            args.file.display(),
            args.capacity,
            args.max_depth,
            args.hash.name()
        );
        let cfg = Config {
            bucket_capacity: args.capacity,
            max_depth: args.max_depth,
            hash: args.hash,
            key_max: args.key_max,
            val_max: args.val_max,
        };
        Index::create(&args.file, &cfg)?
    };

    if let Some(tag) = index.recovered_intent() {
        eprintln!(
            "[ext-hash-index] WARNING: recovered interrupted {} operation on open",
            tag
        );
    }

    let listener = TcpListener::bind(args.addr).await?;
    eprintln!("[ext-hash-index] listening on http://{}", args.addr);
    serve(listener, router(index)).await?;
    Ok(())
}
