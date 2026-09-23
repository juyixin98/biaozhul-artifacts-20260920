//! ehindex：磁盘可扩展哈希页索引（纯后端库）。
//!
//! - [`index::Index`]：磁盘索引核心（分裂 / 合并 / 崩溃恢复）。
//! - [`hash`]：可注入哈希（Fx / 全碰撞 const / 受控碰撞 mod:n）。
//! - [`server`]：基于 Axum 的 HTTP 服务。

pub mod crc32;
pub mod errors;
pub mod hash;
pub mod index;
pub mod page;
pub mod pager;
pub mod server;

pub use errors::IndexError;
pub use hash::HashKind;
pub use index::{Fault, Index, Stats};
