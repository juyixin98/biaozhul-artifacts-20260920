//! 可配置的解析上限。所有上限均为硬限制：达到即返回错误，调用方必须中止。

/// 解析器的资源限制。
///
/// 默认值面向本地测试/小型服务，任何一项都可按需调小或调大；
/// 解析器的内存占用只与 `max_headers_size` 和 boundary 长度有关，**与各正文大小无关**。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 最多允许的 part 数量（默认 32）。
    pub max_parts: usize,
    /// 单个 part 头部块（含结尾 CRLFCRLF）的最大字节数（默认 16 KiB）。
    pub max_headers_size: usize,
    /// 单个 part 正文的最大字节数（默认 8 MiB）。
    pub max_part_size: u64,
    /// 整个 multipart 流的最大字节数（默认 64 MiB）。
    pub max_total_size: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_parts: 32,
            max_headers_size: 16 * 1024,
            max_part_size: 8 * 1024 * 1024,
            max_total_size: 64 * 1024 * 1024,
        }
    }
}

impl Limits {
    /// 所有上限都极小的配置，仅供测试恶意超限场景使用。
    pub fn tiny() -> Self {
        Limits {
            max_parts: 2,
            max_headers_size: 128,
            max_part_size: 10,
            max_total_size: 256,
        }
    }
}
