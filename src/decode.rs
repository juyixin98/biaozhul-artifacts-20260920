//! Incremental byte parser for [`Frame`](crate::frame::Frame) streams.
//!
//! The parser is deliberately written by hand on top of a single bounded
//! `Vec<u8>` accumulator — no external protocol/parser crate does the core
//! work. It supports:
//!
//! * **streaming / half packets** — bytes may arrive one at a time;
//!   [`FrameDecoder::push`] + [`FrameDecoder::next_frame`] keep waiting.
//! * **coalesced packets ("sticky packets")** — one `read` may contain many
//!   frames; call `next_frame` in a loop until it returns `Ok(None)`.
//! * **length caps / bounded memory** — a frame declaring more than
//!   `max_payload` bytes is rejected *from the header alone*, before the body
//!   is read; the internal buffer never holds more than the largest legal
//!   frame plus one `read()` chunk.
//! * **explicit error types** — every failure is a named [`DecodeError`].
//!   Framing-level errors (`is_fatal`) poison the decoder: a TCP-length-prefix
//!   protocol cannot re-synchronise after a corrupt/unknown frame, so the
//!   connection must be torn down. Application-level errors never reach here.

use std::fmt;

use crate::frame::{Frame, FrameKind, Header, HEADER_LEN};

/// Every way incremental decoding can fail.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodeError {
    /// Not enough bytes yet for even the declared frame. Feed more and retry.
    /// This is the normal state while a half packet is outstanding and is
    /// **not** an error condition on the connection.
    NeedMore,
    /// First four bytes were not the `BRPC` magic.
    BadMagic,
    /// Header advertised a protocol version this code does not speak.
    UnsupportedVersion(u8),
    /// Header kind byte is not one of request/response/cancel.
    UnknownKind(u8),
    /// Set flags outside the v1 mask.
    BadFlags(u8),
    /// Declared payload length exceeds the configured cap.
    PayloadTooLarge { declared: usize, max: usize },
    /// Fully assembled frame failed its CRC check.
    CrcMismatch { expected: u32, actual: u32 },
    /// A fatal error already happened; the decoder refuses to continue.
    Poisoned,
}

impl DecodeError {
    /// Fatal errors corrupt frame framing and make the stream unparseable;
    /// the connection must be closed. `NeedMore` is not fatal.
    pub fn is_fatal(&self) -> bool {
        !matches!(self, DecodeError::NeedMore)
    }
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecodeError::NeedMore => write!(f, "incomplete frame: more bytes needed"),
            DecodeError::BadMagic => write!(f, "bad frame magic"),
            DecodeError::UnsupportedVersion(v) => write!(f, "unsupported protocol version {v}"),
            DecodeError::UnknownKind(k) => write!(f, "unknown frame kind {k}"),
            DecodeError::BadFlags(fl) => write!(f, "unsupported flags 0x{fl:02x}"),
            DecodeError::PayloadTooLarge { declared, max } => write!(
                f,
                "payload length {declared} exceeds configured maximum {max}"
            ),
            DecodeError::CrcMismatch { expected, actual } => write!(
                f,
                "crc mismatch: header says {expected:#010x}, computed {actual:#010x}"
            ),
            DecodeError::Poisoned => write!(f, "decoder poisoned by earlier fatal error"),
        }
    }
}

impl std::error::Error for DecodeError {}

/// Incremental, bounded frame decoder.
///
/// Typical loop per TCP connection:
///
/// ```no_run
/// use std::io::Read;
/// use std::net::TcpStream;
///
/// use brpc::decode::FrameDecoder;
/// use brpc::frame::DEFAULT_MAX_PAYLOAD;
///
/// # fn main() -> std::io::Result<()> {
/// let mut stream = TcpStream::connect("127.0.0.1:9000")?;
/// let mut buf = [0u8; 8192];
/// let mut dec = FrameDecoder::new(DEFAULT_MAX_PAYLOAD);
/// loop {
///     let n = stream.read(&mut buf)?;
///     if n == 0 {
///         break;
///     }
///     if dec.push(&buf[..n]).is_err() {
///         break; // fatal framing error: close the connection
///     }
///     while let Some(frame) = dec.next_frame().expect("framing error") {
///         let _ = frame; // dispatch by request_id
///     }
/// }
/// # Ok(())
/// # }
/// ```
#[derive(Debug)]
pub struct FrameDecoder {
    buf: Vec<u8>,
    max_payload: usize,
    /// Byte offset of the frame currently being assembled (0 once consumed).
    start: usize,
    poisoned: bool,
}

impl FrameDecoder {
    /// Decoder rejecting payloads longer than `max_payload` bytes.
    pub fn new(max_payload: usize) -> Self {
        FrameDecoder {
            // Capacity hint: header + a typical payload; grows only as needed
            // up to the cap.
            buf: Vec::with_capacity(HEADER_LEN + 4096),
            max_payload,
            start: 0,
            poisoned: false,
        }
    }

    pub fn max_payload(&self) -> usize {
        self.max_payload
    }

    pub fn is_poisoned(&self) -> bool {
        self.poisoned
    }

    /// Bytes currently retained for the frame being assembled (mainly useful
    /// in tests asserting bounded memory).
    pub fn buffered_len(&self) -> usize {
        self.buf.len() - self.start
    }

    /// Append a `read()` chunk. The chunk may contain any number of frame
    /// boundaries (sticky packets) or end mid-frame (half packet).
    ///
    /// Returns `Err` only for fatal framing errors, after which the decoder is
    /// poisoned; `NeedMore` never occurs here.
    pub fn push(&mut self, chunk: &[u8]) -> Result<(), DecodeError> {
        if self.poisoned {
            return Err(DecodeError::Poisoned);
        }
        // Drop fully-consumed prefix bytes so the accumulator can't grow
        // without bound on a long-lived connection.
        if self.start > 0 {
            self.buf.drain(..self.start);
            self.start = 0;
        }
        self.buf.extend_from_slice(chunk);
        Ok(())
    }

    /// Parse and remove one complete frame if available.
    ///
    /// * `Ok(None)` — waiting for more bytes (half packet); call again after
    ///   the next `push`.
    /// * `Ok(Some(frame))` — a full, CRC-valid frame.
    /// * `Err(..)` — fatal framing error; decoder is poisoned, connection
    ///   must close.
    pub fn next_frame(&mut self) -> Result<Option<Frame>, DecodeError> {
        if self.poisoned {
            return Err(DecodeError::Poisoned);
        }
        let avail = self.buf.len() - self.start;
        if avail < HEADER_LEN {
            return Ok(None);
        }

        let header = match Frame::parse_header(&self.buf[self.start..]) {
            Ok(h) => h,
            Err(DecodeError::NeedMore) => return Ok(None),
            Err(e) => return Err(self.fail(e)),
        };
        if header.payload_len > self.max_payload {
            return Err(self.fail(DecodeError::PayloadTooLarge {
                declared: header.payload_len,
                max: self.max_payload,
            }));
        }

        let total = HEADER_LEN + header.payload_len;
        if avail < total {
            // Half packet: remember we still need `total - avail` bytes, but
            // do not allocate the body speculatively beyond what arrives.
            return Ok(None);
        }

        let frame_bytes = &self.buf[self.start..self.start + total];
        if !Frame::verify_crc(frame_bytes, &header) {
            let actual = recompute_crc(frame_bytes, &header);
            return Err(self.fail(DecodeError::CrcMismatch {
                expected: header.crc,
                actual,
            }));
        }

        let payload = frame_bytes[HEADER_LEN..total].to_vec();
        self.start += total;
        Ok(Some(Frame {
            kind: header.kind,
            flags: header.flags,
            request_id: header.request_id,
            payload,
        }))
    }

    fn fail(&mut self, e: DecodeError) -> DecodeError {
        self.poisoned = true;
        e
    }
}

fn recompute_crc(frame_bytes: &[u8], header: &Header) -> u32 {
    use crate::crc32::Crc32;
    let _ = header.kind; // kind already validated to reach here
    let mut h = Crc32::new();
    h.update(&frame_bytes[4..20]);
    h.update(&frame_bytes[HEADER_LEN..HEADER_LEN + header.payload_len]);
    h.finalize()
}

/// Header subset parse re-export for callers that want routing fields only.
pub fn parse_header(buf: &[u8]) -> Result<Header, DecodeError> {
    Frame::parse_header(buf)
}

/// Convenience: parse kind from a raw byte (subset helper).
pub fn parse_kind(b: u8) -> Option<FrameKind> {
    FrameKind::from_u8(b)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::{Frame, FrameKind};

    fn req(id: u64, body: &[u8]) -> Vec<u8> {
        Frame::new(FrameKind::Request, id, body.to_vec()).encode()
    }

    #[test]
    fn parse_empty_is_need_more() {
        let mut d = FrameDecoder::new(1024);
        assert!(matches!(d.next_frame(), Ok(None)));
        d.push(&[1, 2, 3]).unwrap();
        assert!(matches!(d.next_frame(), Ok(None)));
        assert_eq!(d.buffered_len(), 3);
        assert!(!d.is_poisoned());
    }

    #[test]
    fn byte_by_byte_half_packets() {
        let wire = req(7, b"abc");
        let mut d = FrameDecoder::new(1024);
        // Feed one byte at a time; only the final byte completes a frame.
        for (i, b) in wire.iter().enumerate() {
            d.push(&[*b]).unwrap();
            let f = d.next_frame().unwrap();
            if i + 1 == wire.len() {
                let f = f.expect("frame on last byte");
                assert_eq!(f.request_id, 7);
                assert_eq!(f.payload, b"abc");
            } else {
                assert!(f.is_none(), "premature frame at {}", i + 1);
            }
        }
        assert_eq!(d.buffered_len(), 0);
    }

    #[test]
    fn sticky_packets_many_frames_in_one_chunk() {
        let mut wire = Vec::new();
        for id in 1..=5 {
            wire.extend_from_slice(&req(id, format!("m{id}").as_bytes()));
        }
        let mut d = FrameDecoder::new(1024);
        d.push(&wire).unwrap();
        for id in 1..=5 {
            let f = d.next_frame().unwrap().expect("frame");
            assert_eq!(f.request_id, id);
        }
        assert!(d.next_frame().unwrap().is_none());
        assert_eq!(d.buffered_len(), 0);
    }

    #[test]
    fn two_frames_split_across_pushes() {
        let a = req(11, b"aa");
        let b = req(12, b"bb");
        let cut = a.len() + 3; // split inside frame b's payload
        let mut all = a.clone();
        all.extend_from_slice(&b);
        let mut d = FrameDecoder::new(1024);
        d.push(&all[..cut]).unwrap();
        assert_eq!(d.next_frame().unwrap().unwrap().request_id, 11);
        assert!(d.next_frame().unwrap().is_none());
        d.push(&all[cut..]).unwrap();
        assert_eq!(d.next_frame().unwrap().unwrap().request_id, 12);
        assert_eq!(d.buffered_len(), 0);
    }

    #[test]
    fn header_subset_parse_exposes_routing_fields() {
        let wire = Frame::new(FrameKind::Response, 0x0102_0304_0506_0708, vec![9; 4]).encode();
        let h = Frame::parse_header(&wire).unwrap();
        assert_eq!(h.kind, FrameKind::Response);
        assert_eq!(h.request_id, 0x0102_0304_0506_0708);
        assert_eq!(h.payload_len, 4);
        assert_ne!(h.crc, 0);
    }

    #[test]
    fn bad_magic_is_fatal_and_poisons() {
        let mut bad = req(1, b"x");
        bad[0] ^= 0xFF;
        let mut d = FrameDecoder::new(1024);
        d.push(&bad).unwrap();
        let e = d.next_frame().unwrap_err();
        assert!(matches!(e, DecodeError::BadMagic));
        assert!(e.is_fatal());
        assert!(d.is_poisoned());
        // After poison, even good bytes are refused.
        d.push(&req(2, b"y")).unwrap_err();
        assert!(matches!(d.next_frame(), Err(DecodeError::Poisoned)));
    }

    #[test]
    fn bad_version_unknown_kind_flags_are_named_errors() {
        let mut w = req(1, b"");
        w[4] = 99;
        assert!(matches!(
            Frame::parse_header(&w),
            Err(DecodeError::UnsupportedVersion(99))
        ));
        let mut w = req(1, b"");
        w[5] = 77;
        assert!(matches!(
            Frame::parse_header(&w),
            Err(DecodeError::UnknownKind(77))
        ));
        let mut w = req(1, b"");
        w[6] = 0x80;
        assert!(matches!(
            Frame::parse_header(&w),
            Err(DecodeError::BadFlags(0x80))
        ));
    }

    #[test]
    fn length_cap_rejected_from_header_without_body() {
        // Declare a huge payload but send no body: rejection needs header only.
        let mut w = req(1, b"").to_vec();
        // payload_len lives at bytes 16..20; claim 1 MiB while cap is 16.
        w[16..20].copy_from_slice(&1_048_576u32.to_be_bytes());
        let mut d = FrameDecoder::new(16);
        d.push(&w[..HEADER_LEN]).unwrap();
        let e = d.next_frame().unwrap_err();
        assert!(matches!(
            e,
            DecodeError::PayloadTooLarge {
                declared: 1_048_576,
                max: 16
            }
        ));
        assert!(d.is_poisoned());
    }

    #[test]
    fn exact_cap_payload_accepted() {
        let body = vec![7u8; 16];
        let w = req(1, &body);
        let mut d = FrameDecoder::new(16);
        d.push(&w).unwrap();
        let f = d.next_frame().unwrap().unwrap();
        assert_eq!(f.payload.len(), 16);
    }

    #[test]
    fn crc_flip_detected() {
        let mut w = req(1, b"abcd");
        let last = w.len() - 1;
        w[last] ^= 0x01; // corrupt one payload bit
        let mut d = FrameDecoder::new(1024);
        d.push(&w).unwrap();
        assert!(matches!(
            d.next_frame(),
            Err(DecodeError::CrcMismatch { .. })
        ));
        assert!(d.is_poisoned());
    }

    #[test]
    fn random_splits_always_reassemble() {
        // Deterministic LCG to avoid flaky test timing.
        let mut seed = 0x1234_5678u32;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 17;
            seed ^= seed << 5;
            seed
        };
        let mut wire = Vec::new();
        for id in 0..200u64 {
            wire.extend_from_slice(&req(id, &vec![(id % 251) as u8; (id % 9) as usize]));
        }
        let mut d = FrameDecoder::new(1024);
        let mut pos = 0;
        let mut got = 0u64;
        while pos < wire.len() {
            let step = 1 + (next() % 7) as usize; // 1..=7 bytes per push
            let end = (pos + step).min(wire.len());
            d.push(&wire[pos..end]).unwrap();
            while let Some(f) = d.next_frame().unwrap() {
                assert_eq!(f.request_id, got);
                got += 1;
            }
            pos = end;
        }
        assert_eq!(got, 200);
        assert_eq!(d.buffered_len(), 0);
    }
}
