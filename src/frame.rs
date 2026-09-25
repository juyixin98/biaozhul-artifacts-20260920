//! Binary RPC frame wire format.
//!
//! All integer fields are big-endian. A frame is:
//!
//! ```text
//!  offset  size  field
//!  ------  ----  -----
//!  0       4     magic       = 0x42 52 50 43  ("BRPC")
//!  4       1     version     = 1
//!  5       1     kind        (see FrameKind)
//!  6       1     flags       (bit0: FRAG_MORE reserved for future use; must be 0 on wire v1)
//!  7       1     reserved    = 0
//!  8       8     request_id  (0 = invalid/unused, e.g. not carried yet)
//!  16      4     payload_len (0..=MAX_PAYLOAD)
//!  20      4     crc32       CRC over [version..payload], i.e. header bytes 4..20 + payload
//!  24      N     payload     request/response body
//! ```
//!
//! `HEADER_LEN = 24`. The length field bounds memory: no decoder ever allocates
//! more than `payload_len` for one frame, and the decoder rejects frames whose
//! declared length exceeds the configured cap before reading the payload.

use crate::crc32;
use crate::decode::DecodeError;

/// Fixed frame header size in bytes.
pub const HEADER_LEN: usize = 24;

/// Wire magic "BRPC".
pub const MAGIC: [u8; 4] = [0x42, 0x52, 0x50, 0x43];
/// Only protocol version this implementation speaks.
pub const VERSION: u8 = 1;
/// Largest payload the protocol itself can represent (u32 length field).
pub const MAX_PAYLOAD_HARD: usize = u32::MAX as usize;
/// Default per-frame payload cap used by client and server.
pub const DEFAULT_MAX_PAYLOAD: usize = 1024 * 1024;

/// Frame kind byte.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum FrameKind {
    /// Client → server: a new request.
    Request = 1,
    /// Server → client: a request's response.
    Response = 2,
    /// Client → server: cancel an in-flight request. Empty payload.
    Cancel = 3,
}

impl FrameKind {
    pub fn from_u8(b: u8) -> Option<FrameKind> {
        match b {
            1 => Some(FrameKind::Request),
            2 => Some(FrameKind::Response),
            3 => Some(FrameKind::Cancel),
            _ => None,
        }
    }

    pub fn as_u8(self) -> u8 {
        self as u8
    }
}

/// One decoded (or to-be-encoded) frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub kind: FrameKind,
    pub flags: u8,
    pub request_id: u64,
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn new(kind: FrameKind, request_id: u64, payload: Vec<u8>) -> Self {
        Frame {
            kind,
            flags: 0,
            request_id,
            payload,
        }
    }

    /// Header fields, independent of how the bytes arrived. This is the
    /// *subset parse*: callers that only need routing (kind/id/length) can
    /// inspect a header without committing to the payload.
    pub fn parse_header(buf: &[u8]) -> Result<Header, DecodeError> {
        if buf.len() < HEADER_LEN {
            return Err(DecodeError::NeedMore);
        }
        if buf[0..4] != MAGIC {
            return Err(DecodeError::BadMagic);
        }
        let version = buf[4];
        if version != VERSION {
            return Err(DecodeError::UnsupportedVersion(version));
        }
        let kind = match FrameKind::from_u8(buf[5]) {
            Some(k) => k,
            None => return Err(DecodeError::UnknownKind(buf[5])),
        };
        let flags = buf[6];
        if flags & !SUPPORTED_FLAGS != 0 {
            return Err(DecodeError::BadFlags(flags));
        }
        let request_id = u64::from_be_bytes(buf[8..16].try_into().unwrap());
        let payload_len = u32::from_be_bytes(buf[16..20].try_into().unwrap()) as usize;
        let crc = u32::from_be_bytes(buf[20..24].try_into().unwrap());
        Ok(Header {
            kind,
            flags,
            reserved: buf[7],
            request_id,
            payload_len,
            crc,
        })
    }

    /// Serialize the whole frame (header + payload), computing CRC.
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(HEADER_LEN + self.payload.len());
        out.extend_from_slice(&MAGIC);
        out.push(VERSION);
        out.push(self.kind.as_u8());
        out.push(self.flags);
        out.push(0); // reserved
        out.extend_from_slice(&self.request_id.to_be_bytes());
        out.extend_from_slice(&(self.payload.len() as u32).to_be_bytes());
        // CRC is placed at bytes 20..24; it covers the header fields after
        // the magic (version..length, i.e. assembled bytes 4..20) plus the
        // payload — never the CRC field itself.
        let mut hasher = crc32::Crc32::new();
        hasher.update(&out[4..20]);
        hasher.update(&self.payload);
        let crc = hasher.finalize();
        out.extend_from_slice(&crc.to_be_bytes());
        out.extend_from_slice(&self.payload);
        debug_assert_eq!(out.len(), HEADER_LEN + self.payload.len());
        out
    }

    /// Verify a fully assembled frame's CRC against header+payload bytes.
    pub fn verify_crc(full_frame: &[u8], header: &Header) -> bool {
        if full_frame.len() < HEADER_LEN + header.payload_len {
            return false;
        }
        let payload = &full_frame[HEADER_LEN..HEADER_LEN + header.payload_len];
        let mut hasher = crc32::Crc32::new();
        hasher.update(&full_frame[4..20]);
        hasher.update(payload);
        hasher.finalize() == header.crc
    }
}

/// Bit mask of flags a v1 receiver understands. Others are a protocol error.
pub const SUPPORTED_FLAGS: u8 = 0b0000_0000;

/// Fields extracted from a frame header by [`Frame::parse_header`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub kind: FrameKind,
    pub flags: u8,
    pub reserved: u8,
    pub request_id: u64,
    pub payload_len: usize,
    pub crc: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_layout_and_roundtrip() {
        let f = Frame::new(FrameKind::Request, 0xAABBCCDDEEFF0011, vec![1, 2, 3]);
        let w = f.encode();
        assert_eq!(w.len(), HEADER_LEN + 3);
        assert_eq!(&w[0..4], &MAGIC);
        assert_eq!(w[4], VERSION);
        assert_eq!(w[5], FrameKind::Request as u8);
        assert_eq!(
            u64::from_be_bytes(w[8..16].try_into().unwrap()),
            0xAABBCCDDEEFF0011
        );
        assert_eq!(u32::from_be_bytes(w[16..20].try_into().unwrap()), 3);
        let h = Frame::parse_header(&w).unwrap();
        assert_eq!(h.payload_len, 3);
        assert!(Frame::verify_crc(&w, &h));
        assert_eq!(&w[HEADER_LEN..], &[1, 2, 3]);
    }

    #[test]
    fn all_kinds_roundtrip() {
        for k in [FrameKind::Request, FrameKind::Response, FrameKind::Cancel] {
            let w = Frame::new(k, 5, b"x".to_vec()).encode();
            let h = Frame::parse_header(&w).unwrap();
            assert_eq!(h.kind, k);
            assert!(Frame::verify_crc(&w, &h));
        }
    }

    #[test]
    fn crc_covers_header_and_payload_separately() {
        // Mutating a header byte must fail CRC even with the same body.
        let mut w = Frame::new(FrameKind::Response, 1, vec![9]).encode();
        w[8] ^= 0x01;
        let h = Frame::parse_header(&w).unwrap();
        assert!(!Frame::verify_crc(&w, &h));
    }

    #[test]
    fn header_need_more_with_short_buffer() {
        let w = Frame::new(FrameKind::Cancel, 1, vec![]).encode();
        assert!(matches!(
            Frame::parse_header(&w[..10]),
            Err(DecodeError::NeedMore)
        ));
    }
}
