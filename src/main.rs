//! HTTP server entry point.
//!
//! Configuration via environment variables:
//! * `RCS_DATA_DIR`   - on-disk store directory (default `./data`)
//! * `RCS_BIND`       - listen address (default `127.0.0.1:8080`)
//! * `RCS_RETENTION`  - finalized-upload retention period in seconds (default 3600)

use std::env;
use std::net::SocketAddr;

use refcount_store::{router, Store};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data_dir = env::var("RCS_DATA_DIR").unwrap_or_else(|_| "./data".to_string());
    let bind: SocketAddr = env::var("RCS_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8080".to_string())
        .parse()?;
    let retention: u64 = env::var("RCS_RETENTION")
        .ok()
        .map(|v| v.parse())
        .transpose()?
        .unwrap_or(3600);

    let store = Store::open(&data_dir, retention)?;
    let app = router(store);

    let listener = tokio::net::TcpListener::bind(bind).await?;
    eprintln!("refcount-store listening on http://{bind} (data dir: {data_dir}, upload retention: {retention}s)");
    axum::serve(listener, app).await?;
    Ok(())
}
