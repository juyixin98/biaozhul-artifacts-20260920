//! Incremental UTF-8 validator/decoder.
//!
//! The decoder is an explicit state machine (no use of `std::str` /
//! `String::from_utf8` internally): it accepts arbitrarily chunked input,
//! carries at most 3 pending bytes of state between `feed` calls, and
//! validates every sequence against the tight continuation-byte ranges
//! from Table 3-7 of the Unicode standard, so overlong encodings,
//! surrogates and values above U+10FFFF are all rejected.
//!
//! Memory usage is O(1) in the decoder itself; decoded code points are
//! returned to the caller per `feed` call, and configurable limits bound
//! the total input bytes and total emitted code points.

use crate::error::{DecodeError, ErrorKind};

/// Resource limits enforced by the decoder.
///
/// Limits are always enforced (processing stops) regardless of the
/// configured [`Recovery`] policy, because they exist to protect the
/// host, not to describe data corruption.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Maximum total number of input bytes accepted across all `feed`
    /// calls since the last reset. Default: 64 MiB.
    pub max_input_bytes: u64,
    /// Maximum total number of code points emitted across all `feed`
    /// calls since the last reset. Default: 16 Mi (16_777_216).
    pub max_output_codepoints: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_input_bytes: 64 * 1024 * 1024,
            max_output_codepoints: 1 << 24,
        }
    }
}

impl Limits {
    /// No limits. Use only for trusted input.
    pub fn unlimited() -> Self {
        Limits {
            max_input_bytes: u64::MAX,
            max_output_codepoints: u64::MAX,
        }
    }
}

/// How the decoder reacts to a malformed sequence.
///
/// Neither policy ever performs silent replacement: corrupted bytes are
/// always surfaced as structured [`DecodeError`]s with exact offsets, and
/// no U+FFFD (or any other) replacement character is ever emitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Recovery {
    /// Stop processing at the first malformed sequence. The error is
    /// reported, the offending byte (and the rest of the chunk) is left
    /// unprocessed, and further `feed` calls are rejected until
    /// [`IncrementalDecoder::reset`] is called.
    #[default]
    FailFast,
    /// Report the malformed sequence, drop its already-consumed bytes,
    /// and resynchronize by re-examining the current byte as the start of
    /// a new sequence. Decoding of subsequent valid content continues.
    /// The dropped bytes are *only* discarded after being reported in a
    /// [`DecodeError`] — nothing is silently replaced.
    SkipInvalidBytes,
}

/// Result of one [`IncrementalDecoder::feed`] call.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FeedOutcome {
    /// Code points decoded from this chunk (as `u32` scalar values).
    pub codepoints: Vec<u32>,
    /// Errors encountered while processing this chunk, in order.
    pub errors: Vec<DecodeError>,
    /// `true` if processing stopped before the end of the chunk
    /// (fail-fast error or a limit was hit). The number of bytes actually
    /// consumed from the chunk can be derived from
    /// `decoder.total_consumed()`.
    pub stopped: bool,
}

/// Streaming UTF-8 validator/decoder. See module docs.
#[derive(Debug, Clone)]
pub struct IncrementalDecoder {
    limits: Limits,
    recovery: Recovery,
    // --- in-flight sequence state (at most 3 pending bytes worth) ---
    needed: u8,
    seq_len: u8,
    acc: u32,
    lo: u8,
    hi: u8,
    /// More specific error classification if the *next* continuation byte
    /// fails the range check (set for restricted lead bytes E0/ED/F0/F4).
    pending_reason: Option<ErrorKind>,
    seq_start: u64,
    // --- stream position / counters ---
    offset: u64,
    emitted: u64,
    // --- latches ---
    finished: bool,
    halted: bool,
}

impl IncrementalDecoder {
    /// Create a decoder with the given limits and recovery policy.
    pub fn new(limits: Limits, recovery: Recovery) -> Self {
        IncrementalDecoder {
            limits,
            recovery,
            needed: 0,
            seq_len: 0,
            acc: 0,
            lo: 0x80,
            hi: 0xBF,
            pending_reason: None,
            seq_start: 0,
            offset: 0,
            emitted: 0,
            finished: false,
            halted: false,
        }
    }

    /// Total input bytes consumed since the last reset.
    pub fn total_consumed(&self) -> u64 {
        self.offset
    }

    /// Total code points emitted since the last reset.
    pub fn total_emitted(&self) -> u64 {
        self.emitted
    }

    /// Number of bytes of an incomplete sequence currently buffered
    /// (0..=3). Exposed for diagnostics and testing.
    pub fn pending_bytes(&self) -> u8 {
        if self.needed == 0 {
            0
        } else {
            self.seq_len - self.needed
        }
    }

    /// Forget all state (keeps limits and recovery policy).
    pub fn reset(&mut self) {
        *self = IncrementalDecoder::new(self.limits, self.recovery);
    }

    /// Feed one chunk of input. See [`FeedOutcome`] for the result shape.
    pub fn feed(&mut self, chunk: &[u8]) -> FeedOutcome {
        let mut out = FeedOutcome::default();
        if self.halted || self.finished {
            // The decoder is latched (fail-fast stop, limit hit, or
            // `finish` already called). Refuse further input by reporting
            // `stopped` without consuming anything; call `reset` to reuse.
            out.stopped = true;
            return out;
        }

        let mut i = 0usize;
        while i < chunk.len() {
            if self.offset >= self.limits.max_input_bytes {
                out.errors.push(DecodeError::new(
                    ErrorKind::InputLimitExceeded,
                    self.offset,
                    self.offset,
                ));
                out.stopped = true;
                self.halted = true;
                break;
            }
            let b = chunk[i];
            let mut consumed = true;
            if self.needed == 0 {
                self.step_lead(b, &mut out);
            } else {
                consumed = self.step_continuation(b, &mut out);
            }
            if out.stopped {
                self.halted = true;
                break;
            }
            if consumed {
                i += 1;
                self.offset += 1;
            }
            // If `consumed` is false the same byte is re-examined as the
            // start of a new sequence (resynchronization after an error).
        }
        out
    }

    /// Signal end of stream. Returns `Some(error)` with the absolute byte
    /// offset of the dangling sequence if the stream ended mid-sequence.
    pub fn finish(&mut self) -> Option<DecodeError> {
        self.finished = true;
        if self.needed > 0 {
            let err =
                DecodeError::new(ErrorKind::TruncatedSequence, self.seq_start, self.seq_start);
            self.needed = 0;
            self.acc = 0;
            Some(err)
        } else {
            None
        }
    }

    // --- internal state machine ---

    fn step_lead(&mut self, b: u8, out: &mut FeedOutcome) {
        match b {
            0x00..=0x7F => self.emit(b as u32, out),
            0x80..=0xBF => self.report(ErrorKind::UnexpectedContinuation, out),
            // 0xC0/0xC1 can only ever be an overlong two-byte encoding.
            0xC0..=0xC1 => self.report(ErrorKind::OverlongEncoding, out),
            0xC2..=0xDF => self.begin_sequence(2, (b & 0x1F) as u32, 0x80, 0xBF, None),
            0xE0 => self.begin_sequence(
                3,
                (b & 0x0F) as u32,
                0xA0,
                0xBF,
                Some(ErrorKind::OverlongEncoding),
            ),
            0xE1..=0xEC => self.begin_sequence(3, (b & 0x0F) as u32, 0x80, 0xBF, None),
            0xED => {
                self.begin_sequence(3, (b & 0x0F) as u32, 0x80, 0x9F, Some(ErrorKind::Surrogate))
            }
            0xEE..=0xEF => self.begin_sequence(3, (b & 0x0F) as u32, 0x80, 0xBF, None),
            0xF0 => self.begin_sequence(
                4,
                (b & 0x07) as u32,
                0x90,
                0xBF,
                Some(ErrorKind::OverlongEncoding),
            ),
            0xF1..=0xF3 => self.begin_sequence(4, (b & 0x07) as u32, 0x80, 0xBF, None),
            0xF4 => self.begin_sequence(
                4,
                (b & 0x07) as u32,
                0x80,
                0x8F,
                Some(ErrorKind::OutOfRange),
            ),
            0xF5..=0xFF => self.report(ErrorKind::InvalidLeadByte, out),
        }
    }

    fn begin_sequence(&mut self, seq_len: u8, acc: u32, lo: u8, hi: u8, reason: Option<ErrorKind>) {
        self.needed = seq_len - 1;
        self.seq_len = seq_len;
        self.acc = acc;
        self.lo = lo;
        self.hi = hi;
        self.pending_reason = reason;
        self.seq_start = self.offset;
    }

    /// Returns `false` if the byte was *not* consumed and must be
    /// re-examined as a new lead byte (error resynchronization).
    fn step_continuation(&mut self, b: u8, out: &mut FeedOutcome) -> bool {
        if (self.lo..=self.hi).contains(&b) {
            self.acc = (self.acc << 6) | (b & 0x3F) as u32;
            self.needed -= 1;
            // After the first continuation byte the allowed range always
            // widens to the full continuation range.
            self.lo = 0x80;
            self.hi = 0xBF;
            self.pending_reason = None;
            if self.needed == 0 {
                let cp = self.acc;
                self.acc = 0;
                self.emit(cp, out);
            }
            true
        } else {
            let kind = self
                .pending_reason
                .unwrap_or(ErrorKind::InvalidContinuation);
            self.report(kind, out);
            // In recovery mode the partial sequence is dropped (already
            // reported) and the current byte is re-examined as a lead.
            // In fail-fast mode the loop above breaks on `stopped` before
            // this return value is consulted.
            false
        }
    }

    fn emit(&mut self, cp: u32, out: &mut FeedOutcome) {
        if self.emitted >= self.limits.max_output_codepoints {
            out.errors.push(DecodeError::new(
                ErrorKind::OutputLimitExceeded,
                self.offset,
                self.seq_start,
            ));
            out.stopped = true;
            return;
        }
        out.codepoints.push(cp);
        self.emitted += 1;
    }

    fn report(&mut self, kind: ErrorKind, out: &mut FeedOutcome) {
        out.errors
            .push(DecodeError::new(kind, self.offset, self.seq_start));
        // Abandon any partial sequence; its bytes are accounted for by the
        // error above (sequence_start..offset), never silently replaced.
        self.needed = 0;
        self.acc = 0;
        self.pending_reason = None;
        self.lo = 0x80;
        self.hi = 0xBF;
        if self.recovery == Recovery::FailFast {
            out.stopped = true;
        }
    }
}
