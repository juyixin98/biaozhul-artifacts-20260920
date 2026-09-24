//! 远端构建缓存原型 —— 库入口，供集成测试复用。

pub mod api;
pub mod hash;
pub mod models;
pub mod store;

use std::sync::Arc;

/// 默认单次请求体上限（16 MiB）。
pub const DEFAULT_MAX_BLOB_BYTES: usize = 16 * 1024 * 1024;

/// 构造应用（绑定存储目录），测试与 main 共用。
pub async fn app(store_root: &std::path::Path, max_blob_bytes: usize) -> axum::Router {
    let store = Arc::new(
        store::LocalStore::open(store_root)
            .await
            .expect("open local store"),
    );
    api::router(api::AppState {
        store,
        max_blob_bytes,
    })
}
