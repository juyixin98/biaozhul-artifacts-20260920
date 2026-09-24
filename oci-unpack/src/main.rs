//! HTTP server binary. Pure backend, no UI.
//!
//! Env vars:
//!   OCI_FIXTURES_DIR  (default ./fixtures)
//!   OCI_BUILDS_DIR    (default ./builds)
//!   OCI_BIND          (default 0.0.0.0:8080)
//!   OCI_MAX_COMPRESSED / OCI_MAX_DECOMPRESSED / OCI_MAX_ENTRIES (bytes/count)

use oci_unpack::server::build_router;
use oci_unpack::Limits;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,oci_unpack=debug".into()),
        )
        .init();

    let fixtures_dir = std::env::var("OCI_FIXTURES_DIR").unwrap_or_else(|_| "fixtures".into());
    let builds_dir = std::env::var("OCI_BUILDS_DIR").unwrap_or_else(|_| "builds".into());
    let bind = std::env::var("OCI_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());

    let app = build_router(
        std::path::Path::new(&fixtures_dir),
        std::path::Path::new(&builds_dir),
        limits_from_env(),
    )?;

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!(%bind, fixtures = %fixtures_dir, builds = %builds_dir, "OCI unpack server listening");
    axum::serve(listener, app).await?;
    Ok(())
}

fn limits_from_env() -> Limits {
    let mut lim = Limits::default();
    if let Ok(v) = std::env::var("OCI_MAX_COMPRESSED") {
        if let Ok(n) = v.parse() {
            lim.max_compressed_bytes = n;
        }
    }
    if let Ok(v) = std::env::var("OCI_MAX_DECOMPRESSED") {
        if let Ok(n) = v.parse() {
            lim.max_decompressed_bytes = n;
        }
    }
    if let Ok(v) = std::env::var("OCI_MAX_ENTRIES") {
        if let Ok(n) = v.parse() {
            lim.max_entries = n;
        }
    }
    lim
}
