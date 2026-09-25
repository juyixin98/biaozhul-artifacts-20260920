//! 解析/编码错误类型（明确的有限子集）。

use std::fmt;

/// 增量解析与有限编码过程中可能出现的全部错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DnsError {
    /// 读取位置越过缓冲区末尾（输入被截断）。
    /// 参数：请求读取的绝对偏移；缓冲区长度上限。
    UnexpectedEof { offset: usize, len: usize },
    /// 标签长度超过 63 字节（0x3F），或总名长度超过 255 字节。
    LabelTooLong { len: usize, max: usize },
    /// 标签内容为非 ASCII 字母数字/连字符等时只在文本接口报错；
    /// 线格式本身允许任意字节，因此本变体目前仅由文本构造触发。
    InvalidName(String),
    /// 压缩指针指向报文自身之外（目标偏移 >= 报文长度）。
    PointerOutOfBounds { target: usize, msg_len: usize },
    /// 指针跳转次数超过 [`crate::name::MAX_POINTER_JUMPS`]。
    TooManyPointers { jumps: usize },
    /// 指针形成环（解析过程中第二次到达同一压缩偏移）。
    PointerLoop { offset: u16 },
    /// 遇到 0b01 / 0b10 前缀的保留标签类型，本实现不支持。
    UnsupportedLabelType { prefix: u8 },
    /// 资源记录（或问题/头）在 RDLENGTH 声明的范围内被截断。
    TruncatedRecord { at: usize, declared: usize, actual: usize },
    /// 计数字段（QDCOUNT 等）声明的条目数多于实际存在的条目。
    CountExceedsData { section: &'static str, declared: u16 },
    /// 报文长度超过 [`crate::MAX_MESSAGE_LEN`]（TCP 帧或编码结果）。
    MessageTooLong { len: usize, max: usize },
    /// TCP 长度前缀声明的载荷与实际读取不一致等帧级错误。
    Framing(String),
}

impl fmt::Display for DnsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DnsError::UnexpectedEof { offset, len } => write!(
                f,
                "输入截断：尝试在偏移 {offset} 读取，超出报文长度 {len}"
            ),
            DnsError::LabelTooLong { len, max } => {
                write!(f, "标签/名字过长：长度 {len}，上限 {max}")
            }
            DnsError::InvalidName(s) => write!(f, "非法域名文本：{s:?}"),
            DnsError::PointerOutOfBounds { target, msg_len } => write!(
                f,
                "压缩指针越界：目标偏移 {target}，报文长度 {msg_len}"
            ),
            DnsError::TooManyPointers { jumps } => {
                write!(f, "压缩指针跳转次数超限（{jumps} 次）")
            }
            DnsError::PointerLoop { offset } => {
                write!(f, "检测到压缩指针环（偏移 {offset} 被重复访问）")
            }
            DnsError::UnsupportedLabelType { prefix } => {
                write!(f, "不支持的保留标签类型（高 2 位 = 0b{prefix:02b}）")
            }
            DnsError::TruncatedRecord {
                at,
                declared,
                actual,
            } => write!(
                f,
                "资源记录截断：偏移 {at} 声明 RDLENGTH={declared}，实际只剩 {actual} 字节"
            ),
            DnsError::CountExceedsData {
                section,
                declared,
            } => write!(f, "{section} 计数 {declared} 超出实际数据范围"),
            DnsError::MessageTooLong { len, max } => {
                write!(f, "报文长度 {len} 超过上限 {max}")
            }
            DnsError::Framing(s) => write!(f, "TCP 帧错误：{s}"),
        }
    }
}

impl std::error::Error for DnsError {}

/// 库内统一 Result 别名。
pub type Result<T> = std::result::Result<T, DnsError>;
