//! Wire format and encoder for the custom binary serial protocol.
//!
//! # Frame layout (all multi-byte integers are big-endian)
//!
//! ```text
//!   magic (4)  | length (2) | sequence (2) | payload (length) | crc32 (4)
//!  DE AD BE EF | 00 04      | 00 01        | ...              | little-endian
//! ```
//!
//! * `length` is the payload length in bytes (`0..=65535`).
//! * `sequence` is a `u16` frame counter that wraps from `65535` back to `0`.
//! * `crc32` is CRC-32/ISO-HDLC (see [`crate::crc`]) over `length || sequence || payload`.
//!   The 4-byte checksum is stored little-endian, matching the reflected CRC
//!   convention (zlib/PNG).

use crate::crc;

/// 4-byte start-of-frame delimiter.
pub const MAGIC: [u8; 4] = [0xDE, 0xAD, 0xBE, 0xEF];

/// Fixed header size: magic + length + sequence.
pub const HEADER_LEN: usize = 8;
/// Trailing checksum size.
pub const CRC_LEN: usize = 4;
/// Fixed overhead of any frame on the wire.
pub const FRAME_OVERHEAD: usize = HEADER_LEN + CRC_LEN;

/// Length field is 16 bits, so a payload can never be larger than this.
pub const WIRE_MAX_PAYLOAD: usize = u16::MAX as usize;
/// Conservative default cap applied before any allocation, so a corrupted
/// length field advertising 65535 bytes cannot force a huge allocation.
pub const DEFAULT_MAX_PAYLOAD: usize = 4096;

/// Total on-wire size of a frame carrying `payload_len` bytes.
pub fn frame_total_len(payload_len: usize) -> usize {
    FRAME_OVERHEAD + payload_len
}

/// A successfully decoded frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    /// Wrapping 16-bit sequence number.
    pub sequence: u16,
    /// Payload bytes (already copied out of the parse buffer).
    pub payload: Vec<u8>,
}

/// Errors returned by [`encode_frame`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeError {
    /// Payload does not fit the 16-bit length field.
    PayloadTooLong { len: usize, max: usize },
}

impl std::fmt::Display for EncodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EncodeError::PayloadTooLong { len, max } => write!(
                f,
                "payload length {len} exceeds the {max}-byte protocol limit"
            ),
        }
    }
}

impl std::error::Error for EncodeError {}

/// Encode one frame into a freshly allocated buffer.
///
/// The caller chooses the `max_payload` cap (typically
/// [`DEFAULT_MAX_PAYLOAD`]); the hard protocol cap ([`WIRE_MAX_PAYLOAD`]) is
/// always enforced as well.
pub fn encode_frame(sequence: u16, payload: &[u8], max_payload: usize) -> Result<Vec<u8>, EncodeError> {
    if payload.len() > max_payload || payload.len() > WIRE_MAX_PAYLOAD {
        let max = max_payload.min(WIRE_MAX_PAYLOAD);
        return Err(EncodeError::PayloadTooLong {
            len: payload.len(),
            max,
        });
    }
    let mut out = Vec::with_capacity(FRAME_OVERHEAD + payload.len());
    out.extend_from_slice(&MAGIC);
    out.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    out.extend_from_slice(&sequence.to_be_bytes());
    out.extend_from_slice(payload);
    let c = crc::checksum(&out[MAGIC.len()..]);
    out.extend_from_slice(&c.to_le_bytes());
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_known_layout() {
        let raw = encode_frame(0x0102, &[0xAA, 0xBB, 0xCC], DEFAULT_MAX_PAYLOAD).unwrap();
        assert_eq!(&raw[..8], &[0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x03, 0x01, 0x02]);
        assert_eq!(&raw[8..11], &[0xAA, 0xBB, 0xCC]);
        let c = u32::from_le_bytes(raw[11..15].try_into().unwrap());
        assert_eq!(c, crc::checksum(&raw[4..11]));
        assert_eq!(raw.len(), FRAME_OVERHEAD + 3);
    }

    #[test]
    fn empty_payload_is_valid() {
        let raw = encode_frame(0, &[], DEFAULT_MAX_PAYLOAD).unwrap();
        assert_eq!(raw.len(), FRAME_OVERHEAD);
    }

    #[test]
    fn rejects_oversized_payload() {
        assert!(matches!(
            encode_frame(0, &[0u8; DEFAULT_MAX_PAYLOAD + 1], DEFAULT_MAX_PAYLOAD),
            Err(EncodeError::PayloadTooLong { .. })
        ));
    }
}
