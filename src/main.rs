//! mvcc-kv：单进程 MVCC 键值服务入口。
//!
//! 启动：`cargo run --release`（默认 127.0.0.1:8080），
//! 可用环境变量 `MVCC_LISTEN_ADDR` 覆盖监听地址。

use std::sync::Arc;

use mvcc_kv::{app_router, MvccStore};

#[tokio::main]
async fn main() {
    let addr =
        std::env::var("MVCC_LISTEN_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let db = Arc::new(MvccStore::new());
    let app = app_router(db);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("failed to bind listener");
    println!("mvcc-kv listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}
