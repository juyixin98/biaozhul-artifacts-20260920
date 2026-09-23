//! mvcc-kv 库入口：MVCC 引擎与 Axum 路由，供二进制与集成测试复用。

pub mod api;
pub mod mvcc;

use std::sync::Arc;

pub use mvcc::MvccStore;

/// 构造应用路由的便捷函数。
pub fn app_router(db: Arc<MvccStore>) -> axum::Router {
    api::router(db)
}
