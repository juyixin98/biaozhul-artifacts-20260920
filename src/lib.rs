//! 双页超级块恢复 —— 纯后端持久化引擎。
//!
//! 模块划分：
//! - [`crc`]：CRC-32/IEEE；
//! - [`format`]：磁盘格式（追加数据记录 + 双页超级块）与编解码；
//! - [`io_layer`]：`Storage` 抽象、真实文件后端、可注入断电/半页写的模拟磁盘；
//! - [`store`]：提交协议与恢复选取；
//! - [`selftest`]：崩溃点穷举自检（HTTP `/admin/selftest` 与 CLI 复用）；
//! - [`http_server`]：本地 HTTP 验证入口。

pub mod crc;
pub mod format;
pub mod http_server;
pub mod io_layer;
pub mod selftest;
pub mod store;
