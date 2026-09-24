//! `delta-server` — HTTP frontend for the artifact delta engine.

use artifact_delta::api::app;
use artifact_delta::store::ArtifactStore;
use std::sync::Arc;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=off".into()),
        )
        .init();

    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr = format!("{host}:{port}");

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("failed to bind listener");
    tracing::info!("artifact-delta listening on http://{addr}");

    let router = app(Arc::new(ArtifactStore::new()));
    axum::serve(listener, router).await.expect("server error");
}
