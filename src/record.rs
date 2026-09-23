//! TLS 1.0–1.3 record-layer header parsing (RFC 8446 §5.1).
//!
//! Only the 5-byte record header is parsed here; payloads are never
//! decrypted. The accepted subset of `ContentType` is exactly:
//!
//! | value | meaning              | observer reaction                        |
//! |-------|----------------------|------------------------------------------|
//! | 20    | ChangeCipherSpec     | counted; flips the "encrypted" sentinel  |
//! | 21    | Alert                | counted                                  |
//! | 22    | Handshake            | plaintext fragments fed to reassembly    |
//! | 23    | ApplicationData      | counted as encrypted; never dissected    |

use crate::cursor::{NeedMore, Reader};
use crate::error::{Layer, ParseError, Truncated};

pub const CONTENT_TYPE_CHANGE_CIPHER_SPEC: u8 = 20;
pub const CONTENT_TYPE_ALERT: u8 = 21;
pub const CONTENT_TYPE_HANDSHAKE: u8 = 22;
pub const CONTENT_TYPE_APPLICATION_DATA: u8 = 23;

/// RFC 8446 §5.1: max plaintext fragment 2^14; up to 256 extra for
/// compression/encryption overhead is allowed on the wire.
pub const MAX_RECORD_FRAGMENT_HARD: usize = 16384 + 256;

/// A parsed TLS record header. Bytes are kept as the wire encodes them;
/// nothing about the payload is interpreted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RecordHeader {
    pub content_type: u8,
    pub legacy_major: u8,
    pub legacy_minor: u8,
    pub fragment_length: u16,
}

impl RecordHeader {
    pub fn legacy_version(&self) -> (u8, u8) {
        (self.legacy_major, self.legacy_minor)
    }

    pub fn is_accepted_content_type(&self) -> bool {
        matches!(
            self.content_type,
            CONTENT_TYPE_CHANGE_CIPHER_SPEC
                | CONTENT_TYPE_ALERT
                | CONTENT_TYPE_HANDSHAKE
                | CONTENT_TYPE_APPLICATION_DATA
        )
    }
}

/// Parse one 5-byte record header. A header that runs past the end maps to
/// [`ParseError::Truncated`]; unknown bytes map to the explicit subset
/// errors.
pub(crate) fn parse_record_head(r: &mut Reader<'_>) -> Result<RecordHeader, ParseError> {
    let map = |_: NeedMore| ParseError::Truncated {
        layer: Layer::RecordHeader,
        where_: Truncated::Field,
    };

    let content_type = r.read_u8().map_err(map)?;
    let legacy_major = r.read_u8().map_err(map)?;
    let legacy_minor = r.read_u8().map_err(map)?;
    let fragment_length = r.read_u16().map_err(map)?;

    let header = RecordHeader {
        content_type,
        legacy_major,
        legacy_minor,
        fragment_length,
    };

    if !header.is_accepted_content_type() {
        return Err(ParseError::BadContentType(content_type));
    }
    if legacy_major != 3 || !(1..=3).contains(&legacy_minor) {
        return Err(ParseError::InvalidRecordVersion {
            major: legacy_major,
            minor: legacy_minor,
        });
    }

    Ok(header)
}
