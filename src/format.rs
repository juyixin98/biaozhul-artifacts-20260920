//! 二进制格式常量。完整格式规范见 `docs/FORMAT.md`。
//!
//! 布局概览：
//! ```text
//! +-------------------+------------------------------+
//! | 头部 (12 字节)     | magic "LZSW" | version | ... |
//! +-------------------+------------------------------+
//! | token 流           | 字面量段 / 匹配段 交替出现     |
//! +-------------------+------------------------------+
//! ```

/// 魔数，固定 4 字节。
pub const MAGIC: &[u8; 4] = b"LZSW";
/// 当前格式版本。
pub const VERSION: u8 = 1;
/// 头部总长度（字节）。
pub const HEADER_LEN: usize = 12;
/// 最短匹配长度。
pub const MIN_MATCH: usize = 3;
/// 最长匹配长度：控制字节低 7 位可表示 0..=127，加上 MIN_MATCH。
pub const MAX_MATCH: usize = MIN_MATCH + 127;
/// 单个字面量 token 的最大长度（控制字节 0..=127 表示长度 1..=128）。
pub const MAX_LITERAL_RUN: usize = 128;
/// 最大滑动窗口（回距以 u16 存储）。
pub const MAX_WINDOW: usize = u16::MAX as usize;
/// 默认滑动窗口。
pub const DEFAULT_WINDOW: usize = 4096;
/// 解码器默认输出预算（16 MiB），防止压缩炸弹。
pub const DEFAULT_MAX_OUTPUT: u64 = 16 * 1024 * 1024;
