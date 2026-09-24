//! Incremental, memory-bounded frame parser.
//!
//! Feed arbitrary byte slices via [`FrameParser::feed`] — chunks may split at
//! any point, contain several frames glued together (粘包), partial frames
//! (半包), or arbitrary garbage between frames (噪声). The parser resynchronizes
//! automatically.
//!
//! ## Resynchronization rules
//!
//! * Bytes before the first/next [`MAGIC`] are reported as [`Event::Noise`].
//! * A length field that exceeds the configured cap is rejected *before any
//!   payload allocation* ([`Event::OversizedLength`]); parsing resumes one byte
//!   after that magic so a magic sitting inside the "payload" region is found.
//! * A CRC mismatch ([`Event::BadCrc`]) never consumes the bytes that followed:
//!   parsing resumes one byte after the frame's magic and rescans, so the next
//!   legal frame cannot be swallowed.
//!
//! ## Memory bound
//!
//! The internal buffer never exceeds
//! `HEADER_LEN + max_payload + CRC_LEN + MAGIC.len() - 1` bytes regardless of
//! how large the fed chunks are or what the wire length field advertises.
//!
//! ## Sequence numbers
//!
//! `u16` sequence numbers wrap modulo 65536. After the first frame sets the
//! baseline, a frame is considered in order when it equals `expected`. A
//! forward distance of 1..=32767 reports [`Event::SequenceGap`] with the exact
//! number of missing sequence numbers (this handles wrap, e.g. `65535 -> 1`
//! reports two missing frames, `65535` and `0`). A forward distance > 32767
//! means the frame lies in the past half of the number space:
//! [`Event::OutOfOrder`] is reported and `expected` is left untouched.

use crate::codec::{frame_total_len, Frame, CRC_LEN, HEADER_LEN, MAGIC};
use crate::crc;

/// Largest forward sequence distance that is interpreted as a gap rather than
/// an out-of-order (late/duplicate) frame.
const WRAP_HALF: u16 = 0x7FFF;

/// Something the parser observed while consuming bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A complete frame whose CRC verified.
    Frame {
        frame: Frame,
        /// Absolute byte offset of the frame magic in the fed stream.
        offset: u64,
    },
    /// The declared length exceeds the configured cap; no payload allocated.
    OversizedLength {
        declared_len: u16,
        offset: u64,
    },
    /// Header parsed but the checksum did not match; bytes were kept for
    /// resynchronization and the following frame is unaffected.
    BadCrc {
        sequence: u16,
        declared_len: u16,
        offset: u64,
    },
    /// `count` non-frame bytes skipped starting at `offset`.
    Noise { count: usize, offset: u64 },
    /// One or more in-order sequence numbers were skipped.
    SequenceGap {
        /// Last sequence number that had been delivered before the gap.
        last: u16,
        /// Sequence number of the frame that arrived.
        received: u16,
        /// Number of missing sequence numbers in `expected..=received-1`
        /// (wrapping, at most 32767).
        missing: u16,
        offset: u64,
    },
    /// Frame arrived from the past half of the sequence space (late,
    /// duplicated or reordered); `expected` was not advanced.
    OutOfOrder { received: u16, expected: u16, offset: u64 },
    /// Bytes still held when the stream ended: either an incomplete frame
    /// (`has_magic == true`) or a trailing magic prefix / garbage.
    Truncated {
        bytes: Vec<u8>,
        has_magic: bool,
        offset: u64,
    },
    /// Defensive guard: the bounded buffer could not make progress and was
    /// force-resynchronized. Unreachable for well-formed protocol limits.
    BufferOverflow { offset: u64 },
}

/// Running counters produced by a parse session.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ParseStats {
    pub frames_ok: u64,
    pub bad_crc: u64,
    pub oversized: u64,
    pub sequence_gaps: u64,
    pub out_of_order: u64,
    pub truncated: u64,
    pub buffer_overflows: u64,
    pub bytes_total: u64,
    pub bytes_noise: u64,
}

pub struct FrameParser {
    buf: Vec<u8>,
    max_payload: usize,
    max_buffer: usize,
    expected: Option<u16>,
    /// Absolute stream offset of `buf[0]`.
    stream_pos: u64,
    stats: ParseStats,
}

impl FrameParser {
    /// Create a parser that rejects declared payloads larger than
    /// `max_payload` bytes.
    pub fn new(max_payload: usize) -> Self {
        // u16 length field is the hard protocol ceiling regardless of config.
        let max_payload = max_payload.min(crate::codec::WIRE_MAX_PAYLOAD);
        let max_buffer = HEADER_LEN + max_payload + CRC_LEN + MAGIC.len() - 1;
        Self {
            buf: Vec::with_capacity(max_buffer),
            max_payload,
            max_buffer,
            expected: None,
            stream_pos: 0,
            stats: ParseStats::default(),
        }
    }

    pub fn stats(&self) -> &ParseStats {
        &self.stats
    }

    /// Current size of the bounded internal buffer (mainly for tests).
    pub fn buffer_len(&self) -> usize {
        self.buf.len()
    }

    /// Configured payload cap.
    pub fn max_payload(&self) -> usize {
        self.max_payload
    }

    /// Feed any number of bytes (may be called repeatedly with arbitrary
    /// slicing). Returns the events extracted from this call.
    pub fn feed(&mut self, data: &[u8]) -> Vec<Event> {
        let mut events = Vec::new();
        let mut rest = data;
        while !rest.is_empty() {
            if self.buf.len() >= self.max_buffer {
                // Cannot happen via the normal transitions (an oversized
                // length is rejected at 8 bytes; a valid partial frame is
                // shorter than max_buffer). Keep a hard guard anyway.
                events.push(Event::BufferOverflow {
                    offset: self.stream_pos,
                });
                self.stats.buffer_overflows += 1;
                self.force_resync();
                continue;
            }
            let take = rest.len().min(self.max_buffer - self.buf.len());
            self.buf.extend_from_slice(&rest[..take]);
            rest = &rest[take..];
            self.stats.bytes_total += take as u64;
            self.pump(&mut events);
        }
        events
    }

    /// End the stream: report any held bytes as truncated / trailing noise.
    pub fn finish(&mut self) -> Vec<Event> {
        let mut events = Vec::new();
        if !self.buf.is_empty() {
            let bytes = std::mem::take(&mut self.buf);
            let has_magic = bytes.len() >= MAGIC.len() && bytes[..MAGIC.len()] == MAGIC;
            self.stats.bytes_noise += bytes.len() as u64;
            self.stats.truncated += 1;
            events.push(Event::Truncated {
                bytes,
                has_magic,
                offset: self.stream_pos,
            });
        }
        events
    }

    /// Extract everything that can be extracted from the current buffer.
    fn pump(&mut self, events: &mut Vec<Event>) {
        loop {
            // 1. Locate the next magic; discard leading noise.
            match find_magic(&self.buf) {
                None => {
                    // Retain only the longest trailing magic prefix (0..=3
                    // bytes); every other byte is provably noise.
                    let discard = discard_len_keep_magic_prefix(&self.buf);
                    if discard > 0 {
                        self.emit_noise(discard, events);
                    }
                    return;
                }
                Some(0) => {}
                Some(pos) => {
                    self.emit_noise(pos, events);
                }
            }

            // 2. Wait for the full fixed header.
            if self.buf.len() < HEADER_LEN {
                return;
            }
            let declared_len = u16::from_be_bytes([self.buf[4], self.buf[5]]);
            let sequence = u16::from_be_bytes([self.buf[6], self.buf[7]]);
            let frame_offset = self.stream_pos;

            // 3. Length cap — checked before ANY payload bytes are accumulated.
            if declared_len as usize > self.max_payload {
                events.push(Event::OversizedLength {
                    declared_len,
                    offset: frame_offset,
                });
                self.stats.oversized += 1;
                self.advance(1);
                continue;
            }

            let total = frame_total_len(declared_len as usize);

            // 4. Wait for the rest of the frame (half-packet support).
            if self.buf.len() < total {
                return;
            }

            // 5. Verify the checksum over length || sequence || payload.
            let stored = u32::from_le_bytes([
                self.buf[total - 4],
                self.buf[total - 3],
                self.buf[total - 2],
                self.buf[total - 1],
            ]);
            let actual = crc::checksum(&self.buf[MAGIC.len()..total - CRC_LEN]);
            if stored != actual {
                events.push(Event::BadCrc {
                    sequence,
                    declared_len,
                    offset: frame_offset,
                });
                self.stats.bad_crc += 1;
                // Resync at magic+1; the whole claimed region stays visible
                // so a legal frame embedded after it is never swallowed.
                self.advance(1);
                continue;
            }

            // 6. Deliver the frame.
            let payload = self.buf[HEADER_LEN..total - CRC_LEN].to_vec();
            self.account_sequence(sequence, frame_offset, events);
            events.push(Event::Frame {
                frame: Frame {
                    sequence,
                    payload,
                },
                offset: frame_offset,
            });
            self.stats.frames_ok += 1;
            self.advance(total);
        }
    }

    fn account_sequence(&mut self, seq: u16, frame_offset: u64, events: &mut Vec<Event>) {
        let Some(expected) = self.expected else {
            self.expected = Some(seq.wrapping_add(1));
            return;
        };
        let forward = seq.wrapping_sub(expected);
        if forward == 0 {
            // Exactly the next expected frame.
        } else if forward <= WRAP_HALF {
            // `forward` sequence numbers were skipped: expected ..= seq-1.
            // E.g. expected=11, seq=13 -> {11,12} missing; this also handles
            // wrap: expected=65535, seq=1 -> {65535,0} missing.
            events.push(Event::SequenceGap {
                last: expected.wrapping_sub(1),
                received: seq,
                missing: forward,
                offset: frame_offset,
            });
            self.stats.sequence_gaps += 1;
        } else {
            // Frame belongs to the past half of the number space.
            events.push(Event::OutOfOrder {
                received: seq,
                expected,
                offset: frame_offset,
            });
            self.stats.out_of_order += 1;
            // Do not advance `expected`.
            return;
        }
        self.expected = Some(seq.wrapping_add(1));
    }

    fn emit_noise(&mut self, count: usize, events: &mut Vec<Event>) {
        events.push(Event::Noise {
            count,
            offset: self.stream_pos,
        });
        self.stats.bytes_noise += count as u64;
        self.advance(count);
    }

    /// Drop `n` leading bytes and move the stream position forward.
    fn advance(&mut self, n: usize) {
        if n >= self.buf.len() {
            self.buf.clear();
        } else {
            self.buf.drain(..n);
        }
        self.stream_pos += n as u64;
    }

    fn force_resync(&mut self) {
        let discard = discard_len_keep_magic_prefix(&self.buf).max(1);
        self.stats.bytes_noise += discard as u64;
        self.advance(discard);
    }
}

fn find_magic(buf: &[u8]) -> Option<usize> {
    buf.windows(MAGIC.len()).position(|w| w == MAGIC)
}

/// Number of leading bytes that can be discarded while retaining the longest
/// trailing prefix of [`MAGIC`] (so a magic split across two feeds survives).
fn discard_len_keep_magic_prefix(buf: &[u8]) -> usize {
    for len in (0..MAGIC.len()).rev() {
        if buf.len() >= len && buf[buf.len() - len..] == MAGIC[..len] {
            return buf.len() - len;
        }
    }
    buf.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn magic_prefix_retention() {
        assert_eq!(discard_len_keep_magic_prefix(&[1, 2, 3]), 3);
        assert_eq!(discard_len_keep_magic_prefix(&[1, 2, 0xDE]), 2);
        assert_eq!(discard_len_keep_magic_prefix(&[0xDE]), 0);
        assert_eq!(discard_len_keep_magic_prefix(&[0xDE, 0xAD]), 0);
        assert_eq!(discard_len_keep_magic_prefix(&[9, 0xDE, 0xAD, 0xBE]), 1);
    }
}
