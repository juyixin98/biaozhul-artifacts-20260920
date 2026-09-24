//! repro-pack：确定性制品打包服务（纯后端 HTTP API）。
//!
//! 接口：
//!   GET  /health                → 200 "ok"
//!   POST /pack                  → 打包目录
//!       请求体: {"root": "/abs/or/rel/dir", "paths": ["a.txt", ...]?}
//!       Accept: application/json     → 清单 JSON（含归档 sha256 与大小）
//!       Accept: application/x-tar    → tar 字节流（头部带 X-Archive-Sha256 / X-Archive-Size）

use repro_pack::server::build_router;
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(8080);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind failed");
    eprintln!("repro-pack listening on http://{addr}");
    axum::serve(listener, build_router())
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .expect("serve failed");
}
