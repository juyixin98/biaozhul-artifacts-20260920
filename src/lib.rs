//! 增量 Merkle 校验库
//!
//! 模块：
//! - [`hex_codec`]：十六进制编解码（HTTP/JSON 使用）
//! - [`merkle`]：分块规则、域分离哈希、Merkle 树、范围证明
//! - [`vfs`]：可注入的文件 I/O 层（真实 FS + 故障注入）
//! - [`store`]：文件支持的存储库（chunk/journal/manifest 磁盘格式）
//! - [`http`]：本地 HTTP 验证入口

pub mod hex_codec;
pub mod http;
pub mod merkle;
pub mod store;
pub mod vfs;
