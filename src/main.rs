//! Server entry point.
//!
//! Configuration via environment variables:
//! - `ARTIFACT_BIND`        – bind address (default `127.0.0.1:8080`)
//! - `ARTIFACT_STORE_MAX_BYTES` – in-memory store cap in bytes (default 1 GiB)
//! - `ARTIFACT_MAX_REQUEST_BYTES` – request body cap for raw uploads (default 1 GiB)
//! - `RUST_LOG`             – tracing filter (default `info`)

use std::sync::Arc;

use artifact_delta::api::app;
use artifact_delta::store::Store;
use axum::extract::DefaultBodyLimit;
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let bind = std::env::var("ARTIFACT_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let store_max = parse_env("ARTIFACT_STORE_MAX_BYTES", 1024 * 1024 * 1024)?;
    let request_max = parse_env("ARTIFACT_MAX_REQUEST_BYTES", 1024 * 1024 * 1024)?;

    let store = Arc::new(Store::new(store_max));
    let app = app(store).layer(DefaultBodyLimit::max(request_max));

    let listener = TcpListener::bind(&bind).await?;
    tracing::info!(%bind, store_max, request_max, "artifact-delta listening");
    axum::serve(listener, app).await?;
    Ok(())
}

fn parse_env(name: &str, default: usize) -> Result<usize, Box<dyn std::error::Error>> {
    match std::env::var(name) {
        Ok(v) => Ok(v.trim().parse()?),
        Err(_) => Ok(default),
    }
}
