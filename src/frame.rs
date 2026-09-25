//! Wire frame definition, CRC-32, encoder and the incremental byte parser.
//!
//! # Frame layout (all multi-byte integers are big-endian)
//!
//! ```text
//! offset  size  field
//! 0       2     magic   = 0x42 0x51  ("BQ" — binary rpc)
//! 2       1     version = 0x01
//! 3       1     flags   (bit 0 = REQ_ACK, reserved bits must be zero)
//! 4       1     command (REQUEST=1, RESPONSE=2, CANCEL=3, ERROR=4,
//!                        PING=5, PONG=6; other bytes survive as Unknown)
//! 5       4     request_id
//! 9       4     payload_len
//! 13      N     payload
//! 13+N    4     crc32 over bytes [0 .. 13+N)
//! ```
//!
//! Total header size before payload: 13 bytes. Total trailer: 4 bytes.
//!
//! The parser validates only *framing* (magic, version, declared length, CRC).
//! Semantic fields (command byte, flag bits) are carried to the protocol
//! layer, which decides per-frame recoverable errors (ERROR reply, keep
//! connection) versus fatal framing errors (close connection).

use crate::error::ParseError;

/// First magic byte (0x42 = 'B').
pub const MAGIC0: u8 = 0x42;
/// Second magic byte (0x51 = 'Q').
pub const MAGIC1: u8 = 0x51;
/// Only this protocol version is accepted.
pub const PROTOCOL_VERSION: u8 = 1;
/// Fixed frame header length.
pub const HEADER_LEN: usize = 13;
/// CRC trailer length.
pub const TRAILER_LEN: usize = 4;

/// Default payload cap: 1 MiB.
pub const DEFAULT_MAX_PAYLOAD: u32 = 1024 * 1024;

/// Wire command byte. Unknown wire values are preserved as [`Command::Unknown`]
/// so the protocol layer can answer with an `ERROR` frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Command {
    Request,
    Response,
    Cancel,
    Error,
    Ping,
    Pong,
    /// Any command byte version 1 does not define (carries the raw byte).
    Unknown(u8),
}

impl Command {
    pub fn from_u8(v: u8) -> Command {
        match v {
            1 => Command::Request,
            2 => Command::Response,
            3 => Command::Cancel,
            4 => Command::Error,
            5 => Command::Ping,
            6 => Command::Pong,
            other => Command::Unknown(other),
        }
    }

    pub fn as_u8(self) -> u8 {
        match self {
            Command::Request => 1,
            Command::Response => 2,
            Command::Cancel => 3,
            Command::Error => 4,
            Command::Ping => 5,
            Command::Pong => 6,
            Command::Unknown(v) => v,
        }
    }

    /// Frames that travel client -> server.
    pub fn is_client_to_server(self) -> bool {
        matches!(self, Command::Request | Command::Cancel | Command::Ping)
    }
}

/// Flag bits in the flags byte.
pub mod flags {
    /// Documented v1 flag; a peer may carry it and an endpoint echoes/ignores.
    pub const REQ_ACK: u8 = 0x01;
    /// Mask of every flag bit that version 1 is allowed to carry.
    pub const KNOWN_MASK: u8 = 0x01;
}

/// One parsed frame. Payload is owned (a single bounded copy).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub version: u8,
    pub flags: u8,
    pub command: Command,
    pub request_id: u32,
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn new(command: Command, request_id: u32, payload: Vec<u8>) -> Self {
        Frame {
            version: PROTOCOL_VERSION,
            flags: 0,
            command,
            request_id,
            payload,
        }
    }
}

// ---------------------------------------------------------------------------
// CRC-32 (IEEE 802.3, reflected, init 0xFFFF_FFFF, xorout 0xFFFF_FFFF)
// Table generated at compile time; no external crate is used.
// ---------------------------------------------------------------------------

const fn crc_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = i as u32;
        let mut j = 0;
        while j < 8 {
            crc = if crc & 1 != 0 {
                0xEDB88320 ^ (crc >> 1)
            } else {
                crc >> 1
            };
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
}

static CRC_TABLE: [u32; 256] = crc_table();

/// CRC-32/ISO-HDLC checksum over `data` (same polynomial as zlib/gzip/zip).
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = u32::MAX;
    for &b in data {
        let idx = ((crc ^ b as u32) & 0xFF) as usize;
        crc = CRC_TABLE[idx] ^ (crc >> 8);
    }
    crc ^ u32::MAX
}

// ---------------------------------------------------------------------------
// Encoder
// ---------------------------------------------------------------------------

/// Serialize a frame into `out`. Returns an error tuple (declared, limit) when
/// the payload exceeds `max_payload`.
pub fn encode_frame(frame: &Frame, max_payload: u32, out: &mut Vec<u8>) -> Result<(), (u32, u32)> {
    let len = frame.payload.len() as u64;
    if len > max_payload as u64 {
        return Err((len as u32, max_payload));
    }
    let start = out.len();
    out.reserve(HEADER_LEN + frame.payload.len() + TRAILER_LEN);
    out.extend_from_slice(&[MAGIC0, MAGIC1, PROTOCOL_VERSION]);
    out.push(frame.flags);
    out.push(frame.command.as_u8());
    out.extend_from_slice(&frame.request_id.to_be_bytes());
    out.extend_from_slice(&(frame.payload.len() as u32).to_be_bytes());
    out.extend_from_slice(&frame.payload);
    let crc = crc32(&out[start..start + HEADER_LEN + frame.payload.len()]);
    out.extend_from_slice(&crc.to_be_bytes());
    Ok(())
}

/// Convenience wrapper around [`encode_frame`] returning a fresh buffer.
pub fn encode_frame_vec(frame: &Frame, max_payload: u32) -> Result<Vec<u8>, (u32, u32)> {
    let mut out = Vec::new();
    encode_frame(frame, max_payload, &mut out).map(|()| out)
}

// ---------------------------------------------------------------------------
// Incremental parser
// ---------------------------------------------------------------------------

/// Streaming, allocation-bounded frame parser.
///
/// Feed arbitrary byte chunks with [`IncrementalDecoder::feed`] and pull
/// completed frames with [`IncrementalDecoder::next_frame`]. Partial frames
/// (`NeedMore`) cost nothing extra; arbitrarily many frames may share one
/// buffer (the "sticky packet" case). The buffered byte count never exceeds
/// `max_payload + HEADER_LEN + TRAILER` plus the bytes of one feed chunk.
#[derive(Debug)]
pub struct IncrementalDecoder {
    buf: Vec<u8>,
    /// Start of the unconsumed region; compacted when it drifts past half the
    /// buffer so memory cannot grow frame-by-frame.
    pos: usize,
    max_payload: u32,
    poisoned: bool,
}

impl IncrementalDecoder {
    pub fn new(max_payload: u32) -> Self {
        IncrementalDecoder {
            buf: Vec::new(),
            pos: 0,
            max_payload,
            poisoned: false,
        }
    }

    pub fn with_default_limit() -> Self {
        Self::new(DEFAULT_MAX_PAYLOAD)
    }

    pub fn max_payload(&self) -> u32 {
        self.max_payload
    }

    pub fn is_poisoned(&self) -> bool {
        self.poisoned
    }

    /// Bytes currently held waiting to form (more) frames.
    pub fn buffered_len(&self) -> usize {
        self.buf.len() - self.pos
    }

    /// Capacity of the backing allocation (test/observability helper).
    pub fn capacity(&self) -> usize {
        self.buf.capacity()
    }

    /// Append a TCP read of arbitrary length. Before the payload is copied
    /// further, a header whose declared length exceeds the limit is rejected,
    /// so a peer advertising a giant payload can never force the buffer to
    /// grow to that size (the current chunk is bounded by the OS read size).
    pub fn feed(&mut self, chunk: &[u8]) -> Result<(), ParseError> {
        if self.poisoned {
            return Err(ParseError::Poisoned(Box::new(ParseError::BadMagic)));
        }
        if self.buf.is_empty() {
            self.pos = 0;
        }
        self.buf.extend_from_slice(chunk);
        if self.buffered_len() >= HEADER_LEN {
            self.check_declared_len()?;
        }
        Ok(())
    }

    fn check_declared_len(&self) -> Result<(), ParseError> {
        let hdr = &self.buf[self.pos..self.pos + HEADER_LEN];
        let declared = u32::from_be_bytes([hdr[9], hdr[10], hdr[11], hdr[12]]);
        if declared > self.max_payload {
            return Err(ParseError::PayloadTooLarge {
                declared,
                limit: self.max_payload,
            });
        }
        Ok(())
    }

    /// Try to extract one complete frame.
    ///
    /// Returns [`ParseError::NeedMore`] when more bytes are required, a fatal
    /// [`ParseError`] when framing trust is broken (the decoder then poisons
    /// itself), or a frame on success. Semantic oddities such as an unknown
    /// command byte or flag bits are carried on the frame for the protocol
    /// layer to reject per-frame.
    pub fn next_frame(&mut self) -> Result<Frame, ParseError> {
        if self.poisoned {
            return Err(ParseError::Poisoned(Box::new(ParseError::BadMagic)));
        }
        let avail = self.buffered_len();
        if avail < HEADER_LEN {
            return Err(ParseError::NeedMore);
        }

        let hdr = &self.buf[self.pos..self.pos + HEADER_LEN];
        if hdr[0] != MAGIC0 || hdr[1] != MAGIC1 {
            return self.fail(ParseError::BadMagic);
        }
        let version = hdr[2];
        if version != PROTOCOL_VERSION {
            return self.fail(ParseError::UnsupportedVersion(version));
        }
        let flags = hdr[3];
        let command = Command::from_u8(hdr[4]);
        let request_id = u32::from_be_bytes([hdr[5], hdr[6], hdr[7], hdr[8]]);
        let payload_len = u32::from_be_bytes([hdr[9], hdr[10], hdr[11], hdr[12]]) as usize;
        if payload_len as u32 > self.max_payload {
            return self.fail(ParseError::PayloadTooLarge {
                declared: payload_len as u32,
                limit: self.max_payload,
            });
        }
        let total = HEADER_LEN + payload_len + TRAILER_LEN;
        if avail < total {
            return Err(ParseError::NeedMore);
        }

        let frame_start = self.pos;
        let payload_end = frame_start + HEADER_LEN + payload_len;
        let crc_stored = u32::from_be_bytes([
            self.buf[payload_end],
            self.buf[payload_end + 1],
            self.buf[payload_end + 2],
            self.buf[payload_end + 3],
        ]);
        let crc_computed = crc32(&self.buf[frame_start..payload_end]);
        if crc_stored != crc_computed {
            return self.fail(ParseError::CrcMismatch {
                got: crc_stored,
                computed: crc_computed,
            });
        }

        let payload = self.buf[frame_start + HEADER_LEN..payload_end].to_vec();
        self.advance(total);

        Ok(Frame {
            version,
            flags,
            command,
            request_id,
            payload,
        })
    }

    /// Stream ended: a half-frame left in the buffer is a protocol violation;
    /// an empty buffer is a clean close.
    pub fn finish(&mut self) -> Result<(), ParseError> {
        if self.buffered_len() == 0 {
            Ok(())
        } else {
            self.poisoned = true;
            Err(ParseError::UnexpectedEof)
        }
    }

    fn fail(&mut self, err: ParseError) -> Result<Frame, ParseError> {
        self.poisoned = true;
        Err(err)
    }

    /// Consume `n` bytes of the logical stream, compacting the buffer when the
    /// unconsumed head grows past half capacity.
    fn advance(&mut self, n: usize) {
        self.pos += n;
        debug_assert!(self.pos <= self.buf.len());
        if self.pos == self.buf.len() {
            self.buf.clear();
            self.pos = 0;
        } else if self.pos > 4096 && self.pos >= self.buf.len() / 2 {
            self.buf.drain(..self.pos);
            self.pos = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc_matches_known_vector() {
        // CRC-32/ISO-HDLC of "123456789" is 0xCBF43926.
        assert_eq!(crc32(b"123456789"), 0xCBF43926);
        assert_eq!(crc32(b""), 0x00000000);
    }

    #[test]
    fn roundtrip_simple() {
        let f = Frame::new(Command::Request, 42, b"hello".to_vec());
        let bytes = encode_frame_vec(&f, 1024).unwrap();
        assert_eq!(bytes.len(), HEADER_LEN + 5 + TRAILER_LEN);
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&bytes).unwrap();
        let g = dec.next_frame().unwrap();
        assert_eq!(f, g);
        assert_eq!(dec.next_frame(), Err(ParseError::NeedMore));
    }

    #[test]
    fn byte_by_byte_half_packets() {
        let f = Frame::new(Command::Response, 7, vec![0xAA; 100]);
        let bytes = encode_frame_vec(&f, 1024).unwrap();
        let mut dec = IncrementalDecoder::new(1024);
        let mut got_frame = None;
        for (i, &b) in bytes.iter().enumerate() {
            dec.feed(&[b]).unwrap();
            if i + 1 < bytes.len() {
                assert_eq!(dec.next_frame(), Err(ParseError::NeedMore));
            } else {
                got_frame = Some(dec.next_frame().unwrap());
            }
        }
        assert_eq!(got_frame.unwrap(), f);
        assert_eq!(dec.buffered_len(), 0);
    }

    #[test]
    fn sticky_many_frames_one_buffer() {
        let mut wire = Vec::new();
        for id in 0..50u32 {
            encode_frame(
                &Frame::new(Command::Request, id, format!("payload-{id}").into_bytes()),
                1024,
                &mut wire,
            )
            .unwrap();
        }
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&wire).unwrap();
        for id in 0..50u32 {
            let f = dec.next_frame().unwrap();
            assert_eq!(f.request_id, id);
            assert_eq!(f.payload, format!("payload-{id}").as_bytes());
        }
        assert_eq!(dec.next_frame(), Err(ParseError::NeedMore));
        assert_eq!(dec.buffered_len(), 0);
    }

    #[test]
    fn crc_corruption_is_fatal_and_poisons() {
        let f = Frame::new(Command::Request, 1, vec![1, 2, 3]);
        let mut wire = encode_frame_vec(&f, 1024).unwrap();
        let idx = wire.len() - 1;
        wire[idx] ^= 0xFF;
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&wire).unwrap();
        let err = dec.next_frame().unwrap_err();
        assert!(matches!(err, ParseError::CrcMismatch { .. }));
        assert!(err.is_fatal());
        // Further frames in the same buffer must NOT be parsed: trust is gone.
        assert!(matches!(
            dec.next_frame().unwrap_err(),
            ParseError::Poisoned(_)
        ));
    }

    #[test]
    fn oversized_payload_rejected_from_header() {
        let mut dec = IncrementalDecoder::new(1024);
        let mut hdr = vec![
            MAGIC0,
            MAGIC1,
            PROTOCOL_VERSION,
            0,
            Command::Request.as_u8(),
            0,
            0,
            0,
            1,
            0,
            0,
            0,
            0,
        ];
        hdr[9..13].copy_from_slice(&2048u32.to_be_bytes());
        hdr.extend_from_slice(&[0u8; 4]);
        let err = dec.feed(&hdr).unwrap_err();
        assert!(matches!(
            err,
            ParseError::PayloadTooLarge {
                declared: 2048,
                limit: 1024
            }
        ));
        assert!(err.is_fatal());
        // The advertised payload was never buffered.
        assert!(dec.buffered_len() <= HEADER_LEN + TRAILER_LEN);
    }

    #[test]
    fn bad_version_is_fatal() {
        let mut wire = Vec::new();
        wire.extend_from_slice(&[MAGIC0, MAGIC1, 9, 0, Command::Request.as_u8()]);
        wire.extend_from_slice(&1u32.to_be_bytes());
        wire.extend_from_slice(&0u32.to_be_bytes());
        let crc = crc32(&wire);
        wire.extend_from_slice(&crc.to_be_bytes());
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&wire).unwrap();
        assert!(matches!(
            dec.next_frame().unwrap_err(),
            ParseError::UnsupportedVersion(9)
        ));
    }

    #[test]
    fn bad_magic_is_fatal() {
        let mut dec = IncrementalDecoder::new(1024);
        // Need a full header before magic is validated.
        dec.feed(&[0xDE, 0xAD, 0x01, 0x00, 0x01, 0, 0, 0, 1, 0, 0, 0, 0])
            .unwrap();
        assert!(matches!(
            dec.next_frame().unwrap_err(),
            ParseError::BadMagic
        ));
        assert!(dec.is_poisoned());
    }

    #[test]
    fn unknown_command_survives_as_frame() {
        let f = Frame {
            version: PROTOCOL_VERSION,
            flags: 0,
            command: Command::Unknown(99),
            request_id: 5,
            payload: vec![],
        };
        let wire = encode_frame_vec(&f, 1024).unwrap();
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&wire).unwrap();
        let g = dec.next_frame().unwrap();
        assert_eq!(g.command, Command::Unknown(99));
        assert_eq!(g.command.as_u8(), 99);
    }

    #[test]
    fn unexpected_eof_mid_frame() {
        let f = Frame::new(Command::Ping, 3, vec![9]);
        let wire = encode_frame_vec(&f, 1024).unwrap();
        let mut dec = IncrementalDecoder::new(1024);
        dec.feed(&wire[..wire.len() - 2]).unwrap();
        assert_eq!(dec.next_frame(), Err(ParseError::NeedMore));
        assert!(matches!(
            dec.finish().unwrap_err(),
            ParseError::UnexpectedEof
        ));
        let mut dec2 = IncrementalDecoder::new(1024);
        assert!(dec2.finish().is_ok());
    }
}
