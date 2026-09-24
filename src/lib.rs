//! 不可变对象分块续传服务。
pub mod api;
pub mod error;
pub mod state;
pub mod store;

use std::net::SocketAddr;

pub use api::router;
pub use state::AppState;

/// 默认单个分块上限：64 MiB。
pub const DEFAULT_MAX_CHUNK_SIZE: u64 = 64 * 1024 * 1024;

/// 构建带状态的应用（测试复用入口）。
pub async fn build_app(data_dir: &str, max_chunk_size: u64) -> error::AppResult<axum::Router> {
    let state = AppState::new(data_dir, max_chunk_size).await?;
    Ok(router(state))
}

/// 在指定地址启动服务。
pub async fn serve(addr: SocketAddr, data_dir: &str, max_chunk_size: u64) -> error::AppResult<()> {
    let app = build_app(data_dir, max_chunk_size).await?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(error::AppError::internal)?;
    tracing::info!(%addr, data_dir, max_chunk_size, "listening");
    axum::serve(listener, app)
        .await
        .map_err(error::AppError::internal)
}
