//! 增量构建依赖图规划服务入口。
//!
//! 启动：`cargo run --release`，默认监听 127.0.0.1:8080；
//! 可用环境变量 `BIND_ADDR`（如 `0.0.0.0:9000`）覆盖。

use std::env;

use incr_build_planner::api;
use tokio::net::TcpListener;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr = env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let listener = TcpListener::bind(&addr)
        .await
        .map_err(|e| format!("failed to bind {addr}: {e}"))?;
    println!("incr-build-planner listening on http://{addr}");
    axum::serve(listener, api::app()).await?;
    Ok(())
}
