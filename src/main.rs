//! 存储配额预留服务入口。
//!
//! 启动：
//!   QUOTA_DB=quota.db QUOTA_ADDR=0.0.0.0:8080 cargo run --release
//! 默认监听 127.0.0.1:8080，数据库文件 ./quota.db。

use std::sync::Arc;

use quota_reserve::clock::SystemClock;
use quota_reserve::http::AppState;
use quota_reserve::store::Store;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db_path = std::env::var("QUOTA_DB").unwrap_or_else(|_| "quota.db".to_string());
    let addr = std::env::var("QUOTA_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());

    let store = Store::open(&db_path)?;
    let state = AppState { store, clock: Arc::new(SystemClock::new()) };
    let app = quota_reserve::http::router(state);

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    eprintln!("quota-reserve listening on http://{addr} (db: {db_path})");
    axum::serve(listener, app).await?;
    Ok(())
}
