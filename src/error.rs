//! 统一错误类型。

use std::fmt;

/// 编解码与合并过程中可能出现的错误。
#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    /// 输入字节不足（截断）。
    UnexpectedEof {
        /// 试图读取的字节数。
        wanted: usize,
        /// 实际剩余字节数。
        got: usize,
    },
    /// 魔数不匹配。
    BadMagic,
    /// 版本号不受支持。
    UnsupportedVersion(u8),
    /// 标志位含有未知比特。
    BadFlags(u8),
    /// LEB128 编码超过 10 字节或超出 u64 范围。
    BadVarint,
    /// UTF-8 校验失败。
    BadUtf8,
    /// 段类型未知。
    BadSegmentKind(u8),
    /// 段元数据与载荷不一致。
    BadSegment(&'static str),
    /// 行引用的字典 ID 越界。
    DictIdOutOfRange { id: u64, len: usize },
    /// 字典中出现重复条目（合并时的非法输入）。
    DuplicateDictEntry,
    /// 段编号重复或乱序。
    BadSegmentOrder(u32),
    /// 段数量超出限制。
    TooManySegments { limit: u64, got: u64 },
    /// 累计解码行数超出限制。
    RowsLimitExceeded { limit: u64 },
    /// 累计解码值字节数超出限制。
    ValueBytesLimitExceeded { limit: u64 },
    /// 读取过程中缓冲的数据量超出内存限制。
    MemoryLimitExceeded { limit: usize },
    /// JSON 输入非法。
    BadJson(String),
    /// JSON 请求缺少字段或字段类型不对。
    BadRequest(String),
    /// base64 输入非法。
    BadBase64(String),
    /// 字典基数字过大。
    CardinalityTooLarge { cardinality: usize, max: usize },
    /// 段帧 CRC-32 校验失败（数据损坏）。
    CrcMismatch { expected: u32, actual: u32 },
    /// 段载荷超过允许的最大字节数。
    SegmentTooLarge { size: u64, max: u64 },
    /// 累计保留字典字节数超限。
    DictBytesLimitExceeded { limit: usize },
    /// 底层 I/O 错误。
    Io(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnexpectedEof { wanted, got } => {
                write!(
                    f,
                    "unexpected end of input: wanted {wanted} bytes, got {got}"
                )
            }
            Error::BadMagic => write!(f, "bad magic: expected CDC1"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported format version: {v}"),
            Error::BadFlags(v) => write!(f, "unknown flag bits: {v:#04x}"),
            Error::BadVarint => write!(f, "malformed LEB128 varint"),
            Error::BadUtf8 => write!(f, "invalid UTF-8 in dictionary string"),
            Error::BadSegmentKind(k) => write!(f, "unknown segment kind: {k}"),
            Error::BadSegment(s) => write!(f, "malformed segment: {s}"),
            Error::DictIdOutOfRange { id, len } => {
                write!(
                    f,
                    "dictionary id {id} out of range (dict has {len} entries)"
                )
            }
            Error::DuplicateDictEntry => write!(f, "dictionary contains duplicate entries"),
            Error::BadSegmentOrder(n) => write!(f, "segment number {n} repeated or out of order"),
            Error::TooManySegments { limit, got } => {
                write!(f, "too many segments: {got} > limit {limit}")
            }
            Error::RowsLimitExceeded { limit } => {
                write!(f, "decoded row count exceeds limit of {limit}")
            }
            Error::ValueBytesLimitExceeded { limit } => {
                write!(f, "decoded value byte length exceeds limit of {limit}")
            }
            Error::MemoryLimitExceeded { limit } => {
                write!(f, "buffered memory exceeds limit of {limit} bytes")
            }
            Error::BadJson(s) => write!(f, "invalid JSON: {s}"),
            Error::BadRequest(s) => write!(f, "invalid request: {s}"),
            Error::BadBase64(s) => write!(f, "invalid base64: {s}"),
            Error::CardinalityTooLarge { cardinality, max } => {
                write!(
                    f,
                    "dictionary cardinality {cardinality} exceeds maximum {max}"
                )
            }
            Error::Io(s) => write!(f, "I/O error: {s}"),
            Error::CrcMismatch { expected, actual } => {
                write!(
                    f,
                    "CRC-32 mismatch: stored {expected:#010x}, computed {actual:#010x}"
                )
            }
            Error::SegmentTooLarge { size, max } => {
                write!(f, "segment payload {size} bytes exceeds maximum {max}")
            }
            Error::DictBytesLimitExceeded { limit } => {
                write!(f, "retained dictionary bytes exceed limit of {limit}")
            }
        }
    }
}

impl std::error::Error for Error {}

/// 库内统一 Result 别名。
pub type Result<T> = std::result::Result<T, Error>;
