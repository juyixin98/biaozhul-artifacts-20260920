//! HTTP 服务入口。

use std::net::SocketAddr;

use artifact_promotion::app::{app, AppState};
use artifact_promotion::StoreConfig;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info,artifact_promotion=debug")),
        )
        .init();

    let bind: SocketAddr = std::env::var("BIND_ADDR")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| SocketAddr::from(([127, 0, 0, 1], 8080)));

    let required_proof_kinds = std::env::var("REQUIRED_PROOF_KINDS")
        .ok()
        .map(|s| {
            s.split(',')
                .map(|p| p.trim().to_string())
                .filter(|p| !p.is_empty())
                .collect::<Vec<_>>()
        })
        .filter(|v: &Vec<String>| !v.is_empty())
        .unwrap_or_else(|| {
            vec![
                "unit-test".to_string(),
                "integration-test".to_string(),
                "security-scan".to_string(),
            ]
        });

    tracing::info!(
        "artifact-promotion required proofs: {}",
        required_proof_kinds.join(",")
    );
    let state = AppState::with_config(StoreConfig {
        required_proof_kinds,
    });

    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!("listening on http://{}", listener.local_addr()?);

    axum::serve(listener, app(state)).await?;
    Ok(())
}
