//! 远端构建缓存原型服务入口。
//!
//! 启动：
//!
//! ```bash
//! cargo run --bin rbc-server -- --addr 127.0.0.1:8080 --store ./cache-data
//! # 或用环境变量：RBC_ADDR / RBC_STORE / RBC_MAX_BLOB_BYTES / RUST_LOG
//! ```

use std::net::SocketAddr;
use std::path::PathBuf;

use remote_build_cache::{app, DEFAULT_MAX_BLOB_BYTES};

#[derive(Debug)]
struct Args {
    addr: SocketAddr,
    store: PathBuf,
    max_blob_bytes: usize,
}

fn parse_args() -> Args {
    let mut addr = SocketAddr::from(([127, 0, 0, 1], 8080));
    let mut store = PathBuf::from("./cache-data");
    let mut max_blob_bytes = DEFAULT_MAX_BLOB_BYTES;

    if let Ok(v) = std::env::var("RBC_ADDR") {
        addr = v.parse().expect("invalid RBC_ADDR");
    }
    if let Ok(v) = std::env::var("RBC_STORE") {
        store = PathBuf::from(v);
    }
    if let Ok(v) = std::env::var("RBC_MAX_BLOB_BYTES") {
        max_blob_bytes = v.parse().expect("invalid RBC_MAX_BLOB_BYTES");
    }

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--addr" => addr = it.next().expect("missing --addr value").parse().unwrap(),
            "--store" => store = PathBuf::from(it.next().expect("missing --store value")),
            "--max-blob-bytes" => {
                max_blob_bytes = it
                    .next()
                    .expect("missing --max-blob-bytes value")
                    .parse()
                    .unwrap()
            }
            "-h" | "--help" => {
                println!("Usage: rbc-server [--addr 127.0.0.1:8080] [--store ./cache-data] [--max-blob-bytes 16777216]");
                std::process::exit(0);
            }
            other => panic!("unknown argument: {other}"),
        }
    }

    Args {
        addr,
        store,
        max_blob_bytes,
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = parse_args();
    tracing::info!(
        "remote build cache listening on http://{} (store: {}, max blob: {} bytes)",
        args.addr,
        args.store.display(),
        args.max_blob_bytes
    );

    let app = app(&args.store, args.max_blob_bytes).await;
    let listener = tokio::net::TcpListener::bind(args.addr)
        .await
        .expect("bind listener");
    axum::serve(listener, app).await.expect("server error");
}
