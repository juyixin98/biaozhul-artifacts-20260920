//! Axum server entry point.
//!
//! Env:
//!   COW_DATA_DIR  storage directory (default ./cow-data)
//!   COW_ADDR      listen address (default 0.0.0.0:8080)
//!   COW_CRASH_KILL=1  make injected faults call exit(37) instead of panicking

use std::sync::Arc;

use tokio::net::TcpListener;

use cow_snapshot::server::app;
use cow_snapshot::store::Store;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = std::env::var("COW_DATA_DIR").unwrap_or_else(|_| "cow-data".into());
    let addr = std::env::var("COW_ADDR").unwrap_or_else(|_| "0.0.0.0:8080".into());

    let store = Arc::new(Store::open(&dir)?);
    println!("cow-snapshot: data dir = {dir}");
    println!("cow-snapshot: listening on http://{addr}");
    println!("cow-snapshot: page size 4096, 64 slots per snapshot (256 KiB addressable)");
    let app = app(store);

    let listener = TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
