use std::net::SocketAddr;

use build_cache::{build_router, AppState};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "build_cache=info,tower_http=info".into()),
        )
        .init();

    let data_dir = std::env::var("DATA_DIR").unwrap_or_else(|_| "./data".to_string());
    let bind: SocketAddr = std::env::var("BIND")
        .unwrap_or_else(|_| "127.0.0.1:8080".to_string())
        .parse()
        .expect("BIND must be host:port");

    let state = AppState::new(&data_dir).expect("create data dir");
    let app = build_router(state);

    let listener = tokio::net::TcpListener::bind(bind)
        .await
        .expect("bind listener");
    tracing::info!(%bind, %data_dir, "build cache listening");
    axum::serve(listener, app).await.expect("serve");
}
