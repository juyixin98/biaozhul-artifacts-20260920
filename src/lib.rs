//! cdc —— 列式字符串字典编码（Columnar Dictionary Coding）纯后端库。
//!
//! 模块总览：
//! - [`varint`]：无符号 LEB128（base-128）变长整数编解码
//! - [`table`]：自行实现的去重字符串字典（FNV-1a 哈希 + 开放寻址线性探测）
//! - [`writer`]：流式分段编码器
//! - [`reader`]：流式分段解码器（带输出长度/内存限制）
//! - [`merge`]：多段字典合并与跨段 ID 重映射
//! - [`json`] / [`base64`]：零三方依赖的 JSON 与 base64 实现

pub mod base64;
pub mod control;
pub mod crc32;
pub mod error;
pub mod json;
pub mod merge;
pub mod reader;
pub mod table;
pub mod varint;
pub mod writer;

pub use error::Error;
pub use error::Result;
pub use merge::{merge_sources, MergeStats};
pub use reader::{DecodeLimits, FileReader, Row};
pub use writer::{EncodeLimits, FileWriter, WriteStats};

/// 二进制文件魔数 `CDC1`。
pub const MAGIC: &[u8; 4] = b"CDC1";
/// 格式版本。
pub const FORMAT_VERSION: u8 = 1;
/// 标志位保留字段，当前恒为 0。
pub const FLAGS: u8 = 0;
