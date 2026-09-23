//! 区间锁死锁检测服务 —— HTTP 服务入口。

use std::net::SocketAddr;

use range_lock::engine::Engine;
use range_lock::http::router;

#[tokio::main]
async fn main() {
    // 端口可通过环境变量 RANGE_LOCK_PORT 覆盖，默认 3000。
    let port: u16 = std::env::var("RANGE_LOCK_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3000);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));

    let app = router(Engine::new());
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind listen address");
    println!("range-lock service listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}
