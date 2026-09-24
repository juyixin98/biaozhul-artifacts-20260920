//! 构建输出树合并服务入口。
//!
//! 纯后端 HTTP 服务（Axum），默认监听 `0.0.0.0:8080`，
//! 可用环境变量 `BIND_ADDR`（如 `BIND_ADDR=127.0.0.1:9000`）覆盖。

use std::net::SocketAddr;

use build_merge::http;

#[tokio::main]
async fn main() {
    let bind = std::env::var("BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:8080".to_string());
    let addr: SocketAddr = bind.parse().unwrap_or_else(|e| {
        eprintln!("invalid BIND_ADDR {bind:?}: {e}");
        std::process::exit(2);
    });

    let app = http::router();
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("failed to bind {addr}: {e}");
            std::process::exit(1);
        });
    eprintln!("build-output-merge listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}
