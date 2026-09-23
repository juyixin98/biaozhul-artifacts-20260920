//! TLS 记录层（RFC 8446 §5.1 的 TLSPlaintext 头；同样适用于 TLS 1.2）。
//!
//! 本 crate 只解析**记录头** 5 字节并对 fragment 长度设上限，
//! 不解析/解密 fragment 的通用内容——fragment 仅在明文握手阶段由观察器重组。

use crate::error::ParseError;
use crate::reader::Reader;

/// ChangeCipherSpec
pub const CONTENT_CHANGE_CIPHER_SPEC: u8 = 20;
/// Alert
pub const CONTENT_ALERT: u8 = 21;
/// Handshake
pub const CONTENT_HANDSHAKE: u8 = 22;
/// ApplicationData
pub const CONTENT_APPLICATION_DATA: u8 = 23;

/// 记录头（不含 fragment）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordHeader {
    pub content_type: u8,
    /// legacy_record_version：明文中固定为 0x0301..=0x0304 之一。
    pub version: u16,
    pub fragment_len: u16,
}

impl RecordHeader {
    /// 增量解析一个记录头；不足 5 字节时返回 `Truncated`。
    pub fn parse(buf: &[u8]) -> Result<Self, ParseError> {
        let mut r = Reader::new(buf, "record_header");
        let content_type = r.u8()?;
        if !matches!(
            content_type,
            CONTENT_CHANGE_CIPHER_SPEC
                | CONTENT_ALERT
                | CONTENT_HANDSHAKE
                | CONTENT_APPLICATION_DATA
        ) {
            return Err(ParseError::BadRecordContentType(content_type));
        }
        let version = r.u16()?;
        if !(0x0301..=0x0304).contains(&version) {
            return Err(ParseError::BadRecordVersion {
                version,
                layer: "record",
            });
        }
        let fragment_len = r.u16()?;
        Ok(RecordHeader {
            content_type,
            version,
            fragment_len,
        })
    }

    /// 记录头后 fragment 的起止。
    pub fn fragment_range(&self) -> std::ops::Range<usize> {
        5..5 + self.fragment_len as usize
    }
}

/// ContentType 的可读名称。
pub fn content_type_name(ct: u8) -> &'static str {
    match ct {
        CONTENT_CHANGE_CIPHER_SPEC => "change_cipher_spec",
        CONTENT_ALERT => "alert",
        CONTENT_HANDSHAKE => "handshake",
        CONTENT_APPLICATION_DATA => "application_data",
        other => {
            // 能进入观察器记录阶段的 content_type 都已通过头校验；
            // 其它值只会出现在错误路径。
            debug_assert!(false, "unvalidated content type {other}");
            "unknown"
        }
    }
}
