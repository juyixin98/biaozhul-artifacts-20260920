use std::net::SocketAddr;
use std::path::PathBuf;

use cas_gc::store::{Store, StoreConfig};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

#[tokio::main]
async fn main() {
    let addr: SocketAddr = env_or("CAS_ADDR", "127.0.0.1:8080")
        .parse()
        .expect("CAS_ADDR must be host:port");
    let dir = PathBuf::from(env_or("CAS_DATA_DIR", "./cas-data"));
    let retention: u64 = env_or("CAS_UPLOAD_RETENTION_SECS", "3600")
        .parse()
        .expect("CAS_UPLOAD_RETENTION_SECS must be an integer");

    let store = Store::open(
        &dir,
        StoreConfig {
            default_retention_secs: retention,
        },
    )
    .expect("failed to open store");

    eprintln!("cas-gc listening on http://{addr}");
    eprintln!("data dir: {}", dir.display());
    eprintln!("default upload retention: {retention}s");

    let app = cas_gc::api::router(store);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            eprintln!("shutting down");
        })
        .await
        .expect("server error");
}
