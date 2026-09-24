//! 服务入口。
//!
//! 环境变量：
//! - `LISTEN_ADDR`（默认 0.0.0.0:8080）
//! - `DATA_DIR`（默认 ./data）
//! - `MAX_CHUNK_SIZE`（默认 67108864 = 64 MiB）
//! - `RUST_LOG`（默认 info）
use std::net::SocketAddr;

use chunked_upload::{serve, DEFAULT_MAX_CHUNK_SIZE};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,chunked_upload=debug".into()),
        )
        .init();

    let addr: SocketAddr = std::env::var("LISTEN_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:8080".to_string())
        .parse()
        .expect("invalid LISTEN_ADDR");
    let data_dir = std::env::var("DATA_DIR").unwrap_or_else(|_| "./data".to_string());
    let max_chunk_size = std::env::var("MAX_CHUNK_SIZE")
        .map(|v| v.parse().expect("invalid MAX_CHUNK_SIZE"))
        .unwrap_or(DEFAULT_MAX_CHUNK_SIZE);

    if let Err(e) = serve(addr, &data_dir, max_chunk_size).await {
        tracing::error!(%e, "server exited with error");
        std::process::exit(1);
    }
}
