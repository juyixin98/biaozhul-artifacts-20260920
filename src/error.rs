//! 解析错误类型。
//!
//! 错误分两大类：
//! * [`ParseError::Truncated`] —— 软错误：当前字节不够，后续喂入更多字节可能补齐
//!   （增量解析场景）。连接结束时仍处于该状态则说明报文被截断。
//! * 其余变体 —— 硬错误：线上出现了明确违反所支持子集的结构，观察器进入失败状态，
//!   后续字节不再解析（防止把垃圾/密文继续当握手解释）。

use std::fmt;

/// 观察器支持子集内所有可判定的解析错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// 数据不完整；`what` 指明缺的是哪一层（如 `"record"` / `"handshake_message"`）。
    Truncated { what: &'static str },

    /// 记录层 ContentType 不是 20/21/22/23 之一。
    BadRecordContentType(u8),

    /// 记录层（或其它层）版本不在接受范围内。
    BadRecordVersion { version: u16, layer: &'static str },

    /// TLSPlaintext.fragment 长度超过配置上限。
    RecordTooLarge { len: usize, max: usize },

    /// Handshake 消息长度（u24 声明值）超过配置上限。
    HandshakeTooLarge { len: usize, max: usize },

    /// 声明为 ClientHello 的握手消息体超过配置上限。
    ClientHelloTooLarge { len: usize, max: usize },

    /// 长度前缀声明的长度与外层块实际能提供的字节数不一致。
    /// 例如：扩展块声明 200 字节，但 ClientHello 体内只剩 180 字节。
    LengthMismatch {
        field: &'static str,
        declared: usize,
        actual: usize,
    },

    /// 同一扩展块中出现了两个相同类型（且非 GREASE）的扩展。
    DuplicateExtension { ext_type: u16 },

    /// 字段值在支持子集内不合法（编码正确但语义不接受）。
    InvalidValue {
        field: &'static str,
        reason: &'static str,
    },
}

impl ParseError {
    /// 是否为“数据未到齐”的软错误。
    pub fn is_truncated(&self) -> bool {
        matches!(self, ParseError::Truncated { .. })
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Truncated { what } => {
                write!(f, "truncated: incomplete {what}, need more bytes")
            }
            ParseError::BadRecordContentType(ct) => {
                write!(f, "unsupported TLS record content type: {ct}")
            }
            ParseError::BadRecordVersion { version, layer } => {
                write!(
                    f,
                    "unsupported {layer} layer version: 0x{version:04x} (accepted: 0x0301..=0x0304)"
                )
            }
            ParseError::RecordTooLarge { len, max } => write!(
                f,
                "TLS record fragment too large: declared {len} bytes, limit is {max}"
            ),
            ParseError::HandshakeTooLarge { len, max } => write!(
                f,
                "handshake message too large: declared {len} bytes, limit is {max}"
            ),
            ParseError::ClientHelloTooLarge { len, max } => write!(
                f,
                "ClientHello body too large: declared {len} bytes, limit is {max}"
            ),
            ParseError::LengthMismatch {
                field,
                declared,
                actual,
            } => write!(
                f,
                "length mismatch in `{field}`: declared {declared} byte(s), found {actual}"
            ),
            ParseError::DuplicateExtension { ext_type } => write!(
                f,
                "duplicate extension of type 0x{ext_type:04x} (non-GREASE extensions must appear at most once)"
            ),
            ParseError::InvalidValue { field, reason } => {
                write!(f, "invalid value in `{field}`: {reason}")
            }
        }
    }
}

impl std::error::Error for ParseError {}
