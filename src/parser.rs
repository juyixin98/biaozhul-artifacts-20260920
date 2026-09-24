//! Incremental parser for the custom serial frame protocol.
//!
//! # Wire format (all multi-byte integers big-endian)
//!
//! ```text
//! ┌──────────┬─────────┬──────┬─────────┬──────────────┐
//! │ magic 0  │ magic 1 │ len  │ seq     │ payload      │ CRC32
//! │  0xA5    │  0x5A   │ u16  │ u16     │ len bytes    │ u32
//! └──────────┴─────────┴──────┴─────────┴──────────────┘
//!  \______________________________/     \___________________/
//!       CRC32 input: len..payload_end        frame trailer
//! ```
//!
//! * `len` is the payload length only. Frame total = [`HEADER_LEN`] + len + [`TRAILER_LEN`].
//! * `CRC32` is IEEE CRC-32 (see [`crate::crc`]) computed over bytes from `len`
//!   through the end of the payload — i.e. everything except `magic` and the CRC
//!   itself. Including the declared length in the CRC means a corrupted length
//!   field cannot mislead the parser into accepting the wrong payload.
//!
//! # Properties
//!
//! * **Arbitrary slicing**: feed bytes in chunks of any size (including one byte
//!   at a time); parsing state is carried in [`Parser`].
//! * **Sticky/half packets**: multiple frames in one chunk and one frame spread
//!   over many chunks both work.
//! * **Noise & resync**: bytes before a magic are dropped (reported as
//!   [`Event::Noise`]); a bad-CRC frame is rejected by advancing only one byte,
//!   so a valid frame whose payload *contains* the magic bytes is never lost.
//! * **Bounded memory**: a frame is only reassembled when its declared length is
//!   within the configured maximum, and the parser buffer never exceeds
//!   `HEADER_LEN + max_payload + TRAILER` bytes. A single input chunk larger than
//!   that hard bound is rejected with [`FeedError::ChunkTooLarge`] *before* it is
//!   copied, so a peer cannot force an allocation by declaring a huge length and
//!   cannot grow the buffer by streaming noise.
//! * **Sequence gaps**: see [`Parser::feed`] for the explicit wraparound rule.

use crate::crc::Crc32;

/// First magic byte.
pub const MAGIC0: u8 = 0xA5;
/// Second magic byte.
pub const MAGIC1: u8 = 0x5A;
/// Header length: magic(2) + len(2) + seq(2).
pub const HEADER_LEN: usize = 6;
/// Trailer length: CRC32(4).
pub const TRAILER_LEN: usize = 4;
/// Default maximum payload length in bytes.
pub const DEFAULT_MAX_PAYLOAD: usize = 4096;
/// Maximum representable payload length (u16 length field).
pub const MAX_DECLARED_PAYLOAD: usize = u16::MAX as usize;

/// A successfully parsed frame.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    /// Wire sequence number exactly as received (0..=65535).
    pub seq: u16,
    /// Payload bytes (owned copy; length ≤ configured maximum).
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn new(seq: u16, payload: impl Into<Vec<u8>>) -> Self {
        Self {
            seq,
            payload: payload.into(),
        }
    }
}

/// Everything one call to [`Parser::feed`] can report, in wire order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Event {
    /// A complete frame with a valid CRC.
    Frame(Frame),
    /// `n` noise bytes were skipped while (re)synchronising.
    Noise { bytes: usize },
    /// A frame boundary with a declared payload length larger than the parser's
    /// configured maximum. The magic was consumed and parsing resynchronised;
    /// the bytes that made up the bogus frame are re-scanned for another magic.
    Oversize { declared: u16, max: usize },
    /// A full candidate frame failed the CRC check. The frame was *not* consumed
    /// as a whole: only its first magic byte was discarded, so resync can find a
    /// valid frame embedded behind it. `expected_seq` is the seq the parser is
    /// waiting for next (useful when logging the reject).
    CrcMismatch {
        seq: u16,
        declared_len: u16,
        expected_seq: u16,
    },
    /// Exactly `count` expected sequence numbers were skipped (wraparound-aware),
    /// and the first frame after the gap follows.
    Gap { from: u16, to: u16, count: u16 },
    /// The received seq lies in the past / duplicates under modulo-65536
    /// arithmetic (forward distance > 32767). Frame payload is still delivered,
    /// but the receiver's expectation is not moved.
    SeqRewind { received: u16, expected_seq: u16 },
}

/// Irrecoverable error from [`Parser::feed`]. Nothing was consumed; the caller
/// may retry with a smaller chunk.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FeedError {
    /// The chunk itself is larger than the parser's hard buffer bound. The
    /// parser retains its previous state; split the chunk before retrying.
    ChunkTooLarge { len: usize, bound: usize },
}

impl std::fmt::Display for FeedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FeedError::ChunkTooLarge { len, bound } => write!(
                f,
                "input chunk of {len} bytes exceeds parser hard bound of {bound} bytes"
            ),
        }
    }
}

impl std::error::Error for FeedError {}

/// Incremental, memory-bounded frame parser.
///
/// Holds at most [`Parser::hard_bound`] bytes between calls. Construct once per
/// byte stream/session and call [`Parser::feed`] with bytes as they arrive.
pub struct Parser {
    buf: Vec<u8>,
    max_payload: usize,
    hard_bound: usize,
    expected: Option<u16>,
}

impl Parser {
    /// Parser with the default payload limit ([`DEFAULT_MAX_PAYLOAD`]).
    pub fn new() -> Self {
        Self::with_max_payload(DEFAULT_MAX_PAYLOAD)
    }

    /// Parser with a custom maximum payload length (clamped to `u16::MAX`).
    pub fn with_max_payload(max_payload: usize) -> Self {
        let max_payload = max_payload.min(MAX_DECLARED_PAYLOAD);
        Self {
            buf: Vec::with_capacity(HEADER_LEN + max_payload + TRAILER_LEN),
            max_payload,
            hard_bound: HEADER_LEN + max_payload + TRAILER_LEN,
            expected: None,
        }
    }

    /// Configured maximum payload length.
    pub fn max_payload(&self) -> usize {
        self.max_payload
    }

    /// Maximum bytes the parser will ever buffer (header + max payload + crc).
    pub fn hard_bound(&self) -> usize {
        self.hard_bound
    }

    /// Bytes currently held waiting for more input. Always ≤ [`Self::hard_bound`].
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }

    /// Sequence number the parser expects next (None until the first good frame).
    pub fn expected_seq(&self) -> Option<u16> {
        self.expected
    }

    /// Forget buffered partial bytes (gap/error on the physical line). The
    /// sequence expectation is deliberately kept.
    pub fn reset_buffer(&mut self) {
        self.buf.clear();
    }

    /// Full reset, including sequence tracking (new logical session).
    pub fn reset_all(&mut self) {
        self.buf.clear();
        self.expected = None;
    }

    /// Feed one arbitrary slice of bytes; returns parse events in wire order.
    ///
    /// # Sequence gap rule
    ///
    /// Sequence numbers are `u16` and wrap at 65536. After the first valid frame
    /// sets the expectation, each later valid frame is classified with signed
    /// modular distance `(recv.wrapping_sub(expected) as i16)`:
    ///
    /// | distance | result |
    /// |---|---|
    /// | `0` | in order, no event |
    /// | `1..=32767` | [`Event::Gap`] `count = distance`, then the frame |
    /// | `-32768..=-1` | [`Event::SeqRewind`], frame delivered, expectation unchanged |
    ///
    /// A gap therefore reports exactly how many sequence numbers were lost;
    /// `from`/`to` are reported as plain wrapping values, e.g. `from=65534,
    /// to=1, count=3` means 65535, 0, 1 were expected and 1 was received.
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Event>, FeedError> {
        if chunk.len() > self.hard_bound {
            return Err(FeedError::ChunkTooLarge {
                len: chunk.len(),
                bound: self.hard_bound,
            });
        }

        let mut events = Vec::new();
        let mut rest = chunk;
        // Append in pieces sized to the remaining capacity. The buffer therefore
        // NEVER exceeds hard_bound bytes, even transiently; its allocation was
        // sized to hard_bound up front and never grows.
        while !rest.is_empty() {
            let before = self.buf.len();
            let n = rest.len().min(self.hard_bound - before);
            self.buf.extend_from_slice(&rest[..n]);
            rest = &rest[n..];
            self.process(&mut events);
            // Progress invariant: a completely full buffer always starts either
            // at a valid candidate (consumed as a frame or one byte on CRC fail)
            // or at noise (drained), so process() must have freed >= 1 byte.
            if !rest.is_empty() && self.buf.len() == before + n && n > 0 {
                // Unreachable given the state machine; fail loudly rather than
                // spin if the invariant is ever broken in maintenance.
                panic!("parser made no progress with a full {}-byte buffer", self.hard_bound);
            }
        }
        Ok(events)
    }

    /// Drain every event currently resolvable from `self.buf`.
    fn process(&mut self, events: &mut Vec<Event>) {
        loop {
            // Locate the next magic.
            match find_magic(&self.buf) {
                None => {
                    // No magic anywhere. Preserve a trailing MAGIC0 (might be the
                    // start of magic split across chunks); drop everything else.
                    let keep = if self.buf.last() == Some(&MAGIC0) {
                        1
                    } else {
                        0
                    };
                    let dropped = self.buf.len() - keep;
                    if dropped > 0 {
                        events.push(Event::Noise { bytes: dropped });
                    }
                    self.buf.drain(..dropped);
                    break;
                }
                Some(pos) => {
                    if pos > 0 {
                        events.push(Event::Noise { bytes: pos });
                        self.buf.drain(..pos);
                    }
                    // self.buf now starts with MAGIC.
                    if self.buf.len() < HEADER_LEN {
                        // Partial header: wait for more bytes.
                        break;
                    }
                    let declared =
                        u16::from_be_bytes([self.buf[2], self.buf[3]]) as usize;
                    let frame_total = HEADER_LEN + declared + TRAILER_LEN;

                    if declared > self.max_payload {
                        // Length checked BEFORE any payload-sized allocation: we
                        // only ever copy out payloads <= max_payload.
                        events.push(Event::Oversize {
                            declared: declared as u16,
                            max: self.max_payload,
                        });
                        // Discard only the magic byte, then rescan — the following
                        // bytes may legitimately begin another frame.
                        self.buf.drain(..1);
                        continue;
                    }

                    if self.buf.len() < frame_total {
                        // Partial frame: wait for the rest. Capacity is already
                        // bounded because declared <= max_payload.
                        break;
                    }

                    // Complete candidate: verify CRC before accepting anything.
                    let seq = u16::from_be_bytes([self.buf[4], self.buf[5]]);
                    let crc_wire = u32::from_be_bytes([
                        self.buf[frame_total - 4],
                        self.buf[frame_total - 3],
                        self.buf[frame_total - 2],
                        self.buf[frame_total - 1],
                    ]);
                    let crc_calc =
                        Crc32::checksum(&self.buf[2..HEADER_LEN + declared]);

                    if crc_wire != crc_calc {
                        events.push(Event::CrcMismatch {
                            seq,
                            declared_len: declared as u16,
                            expected_seq: self.expected.unwrap_or(0),
                        });
                        // Drop ONLY the first magic byte. A bad candidate must not
                        // swallow following valid frames (magic may appear in
                        // payloads, noise, etc.).
                        self.buf.drain(..1);
                        continue;
                    }

                    // Valid frame.
                    let payload =
                        self.buf[HEADER_LEN..HEADER_LEN + declared].to_vec();
                    self.buf.drain(..frame_total);
                    self.classify_seq(seq, events);
                    events.push(Event::Frame(Frame { seq, payload }));
                }
            }
        }
    }

    fn classify_seq(&mut self, seq: u16, events: &mut Vec<Event>) {
        match self.expected {
            None => self.expected = Some(seq.wrapping_add(1)),
            Some(expected) => {
                let dist = seq.wrapping_sub(expected) as i16;
                if dist > 0 {
                    events.push(Event::Gap {
                        from: expected,
                        to: seq,
                        count: dist as u16,
                    });
                    self.expected = Some(seq.wrapping_add(1));
                } else if dist < 0 {
                    events.push(Event::SeqRewind {
                        received: seq,
                        expected_seq: expected,
                    });
                    // Do not move the expectation.
                }
                // dist == 0: in order.
                if dist >= 0 {
                    self.expected = Some(seq.wrapping_add(1));
                }
            }
        }
    }
}

impl Default for Parser {
    fn default() -> Self {
        Self::new()
    }
}

/// Find the byte offset of the two-byte magic in `haystack`.
fn find_magic(haystack: &[u8]) -> Option<usize> {
    haystack.windows(2).position(|w| w[0] == MAGIC0 && w[1] == MAGIC1)
}

// ---------------------------------------------------------------------------
// Encoder
// ---------------------------------------------------------------------------

/// Error returned by the frame encoder.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EncodeError {
    /// Payload longer than the u16 length field allows.
    PayloadTooLarge { len: usize },
}

impl std::fmt::Display for EncodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EncodeError::PayloadTooLarge { len } => write!(
                f,
                "payload of {len} bytes exceeds u16 length field (max {MAX_DECLARED_PAYLOAD})"
            ),
        }
    }
}

impl std::error::Error for EncodeError {}

/// Encode one frame. The CRC is really computed over len+seq+payload.
pub fn encode_frame(seq: u16, payload: &[u8]) -> Result<Vec<u8>, EncodeError> {
    if payload.len() > MAX_DECLARED_PAYLOAD {
        return Err(EncodeError::PayloadTooLarge { len: payload.len() });
    }
    let len = payload.len() as u16;
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len() + TRAILER_LEN);
    out.push(MAGIC0);
    out.push(MAGIC1);
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(&seq.to_be_bytes());
    out.extend_from_slice(payload);

    let mut crc = Crc32::new();
    crc.update(&out[2..]); // length + seq + payload (everything after magic)
    out.extend_from_slice(&crc.finalize().to_be_bytes());
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn one_frame(chunks: &[&[u8]]) -> Vec<Frame> {
        let mut p = Parser::new();
        let mut frames = Vec::new();
        for c in chunks {
            for e in p.feed(c).unwrap() {
                if let Event::Frame(f) = e {
                    frames.push(f);
                }
            }
        }
        frames
    }

    #[test]
    fn wire_layout_roundtrip() {
        let wire = encode_frame(0x0102, b"hello").unwrap();
        assert_eq!(wire.len(), HEADER_LEN + 5 + TRAILER_LEN);
        assert_eq!(&wire[0..2], &[MAGIC0, MAGIC1]);
        assert_eq!(&wire[2..4], &[0, 5]);
        assert_eq!(&wire[4..6], &[0x01, 0x02]);
        let frames = one_frame(&[&wire]);
        assert_eq!(frames, vec![Frame::new(0x0102, b"hello".to_vec())]);
    }

    #[test]
    fn empty_payload_is_valid() {
        let wire = encode_frame(7, &[]).unwrap();
        assert_eq!(wire.len(), HEADER_LEN + TRAILER_LEN);
        let frames = one_frame(&[&wire]);
        assert_eq!(frames, vec![Frame::new(7, Vec::new())]);
    }

    #[test]
    fn byte_at_a_time_half_packet() {
        let wire = encode_frame(1, b"abc").unwrap();
        let mut p = Parser::new();
        let mut got = Vec::new();
        for (i, b) in wire.iter().enumerate() {
            let ev = p.feed(std::slice::from_ref(b)).unwrap();
            if i + 1 < wire.len() {
                assert!(ev.is_empty(), "frame must not complete early");
            }
            got.extend(ev);
        }
        assert_eq!(got, vec![Event::Frame(Frame::new(1, b"abc".to_vec()))]);
        assert_eq!(p.buffered_len(), 0);
    }

    #[test]
    fn sticky_packets() {
        let a = encode_frame(0, b"first").unwrap();
        let b = encode_frame(1, b"second").unwrap();
        let c = encode_frame(2, b"third").unwrap();
        let mut stream = Vec::new();
        stream.extend_from_slice(&a);
        stream.extend_from_slice(&b);
        stream.extend_from_slice(&c);
        let frames = one_frame(&[&stream]);
        assert_eq!(frames.len(), 3);
        assert_eq!(frames[1].payload, b"second");
    }

    #[test]
    fn payload_containing_magic() {
        let tricky = vec![MAGIC0, MAGIC1, 0, 0, MAGIC1, MAGIC0, 0xA5, 0xA5, 0x5A];
        let wire = encode_frame(42, &tricky).unwrap();
        let frames = one_frame(&[&wire]);
        assert_eq!(frames, vec![Frame::new(42, tricky)]);
    }
}
