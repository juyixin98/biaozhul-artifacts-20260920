//! 存储配额预留服务库。
//!
//! - [`clock`]：可注入时钟（系统时钟 / 测试假时钟）。
//! - [`store`]：SQLite 持久化与全部配额状态转换。
//! - [`http`]：Axum 路由与 HTTP 接口。

pub mod clock;
pub mod http;
pub mod store;
