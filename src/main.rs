use lockmgr::api;
use lockmgr::lock_manager::{Config, LockManager};
use std::sync::Arc;

#[tokio::main]
async fn main() {
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(3000);
    let config = Config {
        num_buckets: std::env::var("LOCK_BUCKETS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(64),
        max_wait_edges: std::env::var("MAX_WAIT_EDGES")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(10_000),
        default_timeout_ms: std::env::var("LOCK_TIMEOUT_MS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5_000),
    };
    let mgr = Arc::new(LockManager::new(config.clone()));
    let app = api::router(mgr);
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .expect("bind failed");
    println!(
        "lockmgr listening on 0.0.0.0:{port} (buckets={}, max_wait_edges={}, default_timeout={}ms)",
        config.num_buckets, config.max_wait_edges, config.default_timeout_ms
    );
    axum::serve(listener, app).await.expect("server failed");
}
