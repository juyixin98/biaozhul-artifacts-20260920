//! 错误类型。

use std::fmt;

/// 库内所有可恢复错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// 输入超过调用方给出的内存 / 长度上限。
    LimitExceeded {
        /// 上限（字节，对于位级字段为位数）。
        limit: u64,
        /// 实际需要或观测到的值。
        actual: u64,
        /// 人类可读的被限量名称。
        what: &'static str,
    },
    /// 魔数不匹配。
    BadMagic,
    /// 版本号不受支持。
    UnsupportedVersion(u8),
    /// 输入在完整头部之前结束。
    TruncatedHeader,
    /// 载荷声明长度与头部不一致 / 位流意外结束。
    TruncatedStream,
    /// 头部保留字段非零。
    ReservedBitsSet(u8),
    /// 头部声明的原始长度超过 `max_output_bytes`。
    OutputTooLong { declared: u64, limit: u64 },
    /// 码长表条目非法（0 长度出现在已声明的符号范围内的非法位置、长度超界等）。
    InvalidLengthTable(&'static str),
    /// 码长表过度订阅：Kraft 不等式超过 1（canonical 起始码溢出）。
    Oversubscribed,
    /// 码长表不完整：Kraft 和小于 1（存在未被任何码字覆盖的位串）。
    IncompleteCode,
    /// 有效载荷解码出的字节数与头部声明的原始长度不符。
    LengthMismatch { declared: u64, decoded: u64 },
    /// 码流在一个码字中间结束。
    UnexpectedEndOfCode,
    /// 位流走到了码表未定义的位串（仅不完整码表可能发生）。
    UndefinedCodeword,
    /// 解码出的有效数据之后仍存在非零有效位（拒绝尾随数据）。
    TrailingBits,
    /// BFINAL 块之后仍有多余字节。
    TrailingData,
    /// 末字节中超出 `payload_bits` 的填充位不为 0。
    NonZeroPadding,
    /// 规范码（canonical code）在规定长度内容纳不下。
    CodeDoesNotFit,
    /// 频率表逻辑错误（理论上不会出现，属于保护性检查）。
    FrequencyLogic(&'static str),
    /// 空输入但调用方强制要求编码（空输入应走单独的空载荷路径）。
    EmptyInput,
    /// JSON 控制入口的请求非法。
    BadRequest(String),
    /// 底层 I/O 错误（携带 kind 文本）。
    Io(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::LimitExceeded {
                limit,
                actual,
                what,
            } => write!(
                f,
                "limit exceeded for {what}: limit={limit}, actual={actual}"
            ),
            Error::BadMagic => write!(f, "bad magic bytes"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported format version {v}"),
            Error::TruncatedHeader => write!(f, "truncated header"),
            Error::TruncatedStream => write!(f, "truncated bit stream"),
            Error::ReservedBitsSet(b) => write!(f, "reserved bits set: 0x{b:02x}"),
            Error::OutputTooLong { declared, limit } => {
                write!(f, "declared output length {declared} exceeds limit {limit}")
            }
            Error::InvalidLengthTable(s) => write!(f, "invalid code length table: {s}"),
            Error::Oversubscribed => {
                write!(f, "oversubscribed code: Kraft sum exceeds 1")
            }
            Error::IncompleteCode => write!(f, "incomplete code: Kraft sum below 1"),
            Error::LengthMismatch { declared, decoded } => write!(
                f,
                "length mismatch: header declared {declared}, decoded {decoded}"
            ),
            Error::UnexpectedEndOfCode => write!(f, "unexpected end of stream mid-code"),
            Error::UndefinedCodeword => write!(f, "bit string undefined by code table"),
            Error::TrailingBits => write!(f, "non-zero bits after declared payload end"),
            Error::TrailingData => write!(f, "trailing data after final block"),
            Error::NonZeroPadding => write!(f, "non-zero padding bits in final byte"),
            Error::CodeDoesNotFit => write!(f, "canonical codes do not fit in their lengths"),
            Error::FrequencyLogic(s) => write!(f, "frequency logic error: {s}"),
            Error::EmptyInput => write!(f, "empty input"),
            Error::BadRequest(s) => write!(f, "bad request: {s}"),
            Error::Io(s) => write!(f, "I/O error: {s}"),
        }
    }
}

impl std::error::Error for Error {}

/// 库专用 Result。
pub type Result<T> = std::result::Result<T, Error>;
