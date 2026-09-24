//! 服务入口。默认监听 0.0.0.0:8080,可用环境变量 PORT 覆盖。

use artifact_promotion::{api, store::Store};
use std::sync::Arc;

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr = format!("0.0.0.0:{}", port);

    let store = Arc::new(Store::new());
    let app = api::app(store);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {}: {}", addr, e));
    eprintln!("artifact-promotion service listening on http://{}", addr);
    axum::serve(listener, app).await.expect("server error");
}
