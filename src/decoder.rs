//! Incremental, dependency-free UTF-8 decoder.
//!
//! The decoder is a small state machine with O(1) memory: at most three
//! bytes of a pending multi-byte sequence plus the running code point are
//! held between [`Decoder::feed`] calls, so multi-byte sequences may be
//! split across chunk boundaries at any byte offset.
//!
//! Strictness (RFC 3629 / Unicode "best practice"):
//! * overlong encodings are rejected (`C0 80`, `E0 80 ..`, `F0 80 ..`, ...),
//! * UTF-16 surrogate halves U+D800..=U+DFFF are rejected (`ED A0..BF ..`),
//! * values above U+10FFFF are rejected (`F4 90.. ..`, `F5..F7 ..`),
//! * a sequence left unfinished at end of input is reported with the
//!   absolute byte offset of its lead byte.
//!
//! Corrupted input is never silently replaced: invalid bytes surface as
//! [`DecodeError`] entries. With [`ErrorPolicy::Collect`] decoding
//! resynchronises and continues, but the invalid bytes are dropped (not
//! substituted) and always reported.

use crate::encoder;
use crate::error::{DecodeError, ErrorKind};

/// What to do when an invalid byte is encountered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorPolicy {
    /// Stop at the first error; the remainder of the input is not consumed.
    Abort,
    /// Record the error, drop the invalid bytes, resynchronise at the next
    /// possible lead byte and keep decoding. The output contains exactly the
    /// valid portions of the stream; every corrupted region appears in
    /// `errors`. Nothing is silently replaced.
    Collect,
}

/// Default cap on decoded output length, in code points.
pub const DEFAULT_MAX_OUTPUT_CODEPOINTS: u64 = 1_000_000;

/// The result of a finished decoding run.
#[derive(Debug, Clone)]
pub struct DecodeReport {
    /// Decoded output bytes (valid UTF-8 by construction). Empty when the
    /// decoder was created with `emit_output == false`.
    pub output: Vec<u8>,
    /// Every error encountered, in stream order. Empty means success.
    pub errors: Vec<DecodeError>,
    /// Number of input bytes actually processed. Equals the input length
    /// unless decoding halted early (abort policy or output limit).
    pub consumed_bytes: u64,
    /// Number of code points emitted.
    pub output_codepoints: u64,
    /// True when decoding stopped because the output limit was exceeded.
    pub truncated: bool,
    /// Bytes of an unfinished sequence still buffered at the end
    /// (non-zero only when input ended mid-sequence).
    pub pending_sequence_bytes: u8,
}

impl DecodeReport {
    /// True when no error of any kind was recorded.
    pub fn ok(&self) -> bool {
        self.errors.is_empty()
    }
}

/// Incremental UTF-8 decoder. See the module docs for the rules enforced.
pub struct Decoder {
    policy: ErrorPolicy,
    max_output: u64,
    emit_output: bool,
    // --- pending sequence state (reset when a sequence completes/fails) ---
    /// Continuation bytes still expected.
    needed: u8,
    /// Code point accumulated so far.
    cp: u32,
    /// Legal range for the *next* continuation byte (restricted for the
    /// first continuation of E0/ED/F0/F4 leads, 0x80..=0xBF otherwise).
    cont_min: u8,
    cont_max: u8,
    /// Error kind to report when the first continuation byte is inside
    /// 0x80..=0xBF but outside the restricted range.
    range_violation: ErrorKind,
    /// Absolute offset of the pending sequence's lead byte.
    seq_start: u64,
    /// Bytes of the pending sequence consumed so far, lead included.
    seq_len: u8,
    // --- stream state ---
    /// Absolute offset of the next input byte.
    next_offset: u64,
    output: Vec<u8>,
    output_codepoints: u64,
    errors: Vec<DecodeError>,
    truncated: bool,
    halted: bool,
}

impl Decoder {
    /// A decoder that aborts on the first error, emits output, and applies
    /// [`DEFAULT_MAX_OUTPUT_CODEPOINTS`].
    pub fn new(policy: ErrorPolicy) -> Self {
        Self::with_options(policy, DEFAULT_MAX_OUTPUT_CODEPOINTS, true)
    }

    /// A decoder that validates without buffering decoded output
    /// (constant memory regardless of input size).
    pub fn validator(policy: ErrorPolicy) -> Self {
        Self::with_options(policy, DEFAULT_MAX_OUTPUT_CODEPOINTS, false)
    }

    pub fn with_options(policy: ErrorPolicy, max_output_codepoints: u64, emit_output: bool) -> Self {
        Decoder {
            policy,
            max_output: max_output_codepoints,
            emit_output,
            needed: 0,
            cp: 0,
            cont_min: 0x80,
            cont_max: 0xBF,
            range_violation: ErrorKind::InvalidContinuationByte,
            seq_start: 0,
            seq_len: 0,
            next_offset: 0,
            output: Vec::new(),
            output_codepoints: 0,
            errors: Vec::new(),
            truncated: false,
            halted: false,
        }
    }

    /// Bytes of a multi-byte sequence currently held across a chunk
    /// boundary (0 when the decoder is at a code point boundary).
    pub fn pending_sequence_bytes(&self) -> u8 {
        self.seq_len
    }

    /// Feed one chunk of input. Sequences may straddle chunk boundaries.
    pub fn feed(&mut self, chunk: &[u8]) {
        for &b in chunk {
            if self.halted {
                return;
            }
            self.step(b);
            self.next_offset += 1;
        }
    }

    /// Signal end of input and collect the report. An unfinished pending
    /// sequence is reported as [`ErrorKind::IncompleteSequence`] with the
    /// absolute offset of its lead byte.
    pub fn finish(self) -> DecodeReport {
        self.into_report(true)
    }

    /// Collect the report without treating a pending sequence as an error
    /// (for non-final chunks in a streaming session). The pending bytes
    /// are still visible via [`DecodeReport::pending_sequence_bytes`].
    pub fn finish_non_final(self) -> DecodeReport {
        self.into_report(false)
    }

    fn into_report(mut self, is_final: bool) -> DecodeReport {
        if is_final && self.needed > 0 && !self.halted {
            self.errors.push(DecodeError {
                kind: ErrorKind::IncompleteSequence,
                offset: self.seq_start,
                sequence_start: self.seq_start,
                byte: None,
                sequence_len: self.seq_len,
                detail: format!(
                    "input ended with {} of {} bytes of a multi-byte sequence",
                    self.seq_len,
                    self.seq_len + self.needed
                ),
            });
        }
        DecodeReport {
            output: self.output,
            errors: self.errors,
            consumed_bytes: self.next_offset,
            output_codepoints: self.output_codepoints,
            truncated: self.truncated,
            pending_sequence_bytes: self.seq_len,
        }
    }

    fn step(&mut self, b: u8) {
        if self.needed == 0 {
            self.step_ground(b);
        } else {
            self.step_continuation(b);
        }
    }

    fn step_ground(&mut self, b: u8) {
        match b {
            0x00..=0x7F => self.emit(b as u32),
            0x80..=0xBF => self.fail(
                ErrorKind::InvalidLeadByte,
                Some(b),
                "stray continuation byte without a lead byte".to_string(),
            ),
            0xC0..=0xC1 => self.fail(
                ErrorKind::OverlongEncoding,
                Some(b),
                "lead byte is only ever used for overlong encodings".to_string(),
            ),
            0xC2..=0xDF => self.begin(1, b & 0x1F, 0x80, 0xBF, ErrorKind::InvalidContinuationByte),
            0xE0 => self.begin(2, b & 0x0F, 0xA0, 0xBF, ErrorKind::OverlongEncoding),
            0xE1..=0xEC | 0xEE..=0xEF => {
                self.begin(2, b & 0x0F, 0x80, 0xBF, ErrorKind::InvalidContinuationByte)
            }
            0xED => self.begin(2, b & 0x0F, 0x80, 0x9F, ErrorKind::SurrogateCodePoint),
            0xF0 => self.begin(3, b & 0x07, 0x90, 0xBF, ErrorKind::OverlongEncoding),
            0xF1..=0xF3 => self.begin(3, b & 0x07, 0x80, 0xBF, ErrorKind::InvalidContinuationByte),
            0xF4 => self.begin(3, b & 0x07, 0x80, 0x8F, ErrorKind::CodePointOutOfRange),
            0xF5..=0xF7 => self.fail(
                ErrorKind::CodePointOutOfRange,
                Some(b),
                "lead byte would encode a value above U+10FFFF".to_string(),
            ),
            0xF8..=0xFF => self.fail(
                ErrorKind::InvalidLeadByte,
                Some(b),
                "byte is never valid in UTF-8".to_string(),
            ),
        }
    }

    fn step_continuation(&mut self, b: u8) {
        if b >= self.cont_min && b <= self.cont_max {
            self.cp = (self.cp << 6) | u32::from(b & 0x3F);
            self.needed -= 1;
            self.seq_len += 1;
            // Only the first continuation byte of a sequence is restricted.
            self.cont_min = 0x80;
            self.cont_max = 0xBF;
            self.range_violation = ErrorKind::InvalidContinuationByte;
            if self.needed == 0 {
                let cp = self.cp;
                self.emit(cp);
                self.seq_len = 0;
            }
        } else {
            let kind = if (0x80..=0xBF).contains(&b) {
                // A continuation byte, but outside the restricted range of
                // this lead byte: overlong, surrogate or out-of-range.
                self.range_violation
            } else {
                ErrorKind::InvalidContinuationByte
            };
            let detail = format!(
                "expected continuation byte in 0x{:02X}..=0x{:02X}, got 0x{:02X}",
                self.cont_min, self.cont_max, b
            );
            self.fail(kind, Some(b), detail);
            // Resynchronise: a non-continuation byte may itself start a new
            // sequence. A stray continuation byte is simply dropped (and was
            // already reported above).
            if self.policy == ErrorPolicy::Collect && !self.halted && !(0x80..=0xBF).contains(&b) {
                self.step_ground(b);
            }
        }
    }

    fn begin(&mut self, needed: u8, cp_bits: u8, cont_min: u8, cont_max: u8, violation: ErrorKind) {
        self.needed = needed;
        self.cp = u32::from(cp_bits);
        self.cont_min = cont_min;
        self.cont_max = cont_max;
        self.range_violation = violation;
        self.seq_start = self.next_offset;
        self.seq_len = 1;
    }

    fn fail(&mut self, kind: ErrorKind, byte: Option<u8>, detail: String) {
        let (sequence_start, sequence_len) = if self.seq_len > 0 {
            (self.seq_start, self.seq_len)
        } else {
            (self.next_offset, 1)
        };
        self.errors.push(DecodeError {
            kind,
            offset: self.next_offset,
            sequence_start,
            byte,
            sequence_len,
            detail,
        });
        self.needed = 0;
        self.seq_len = 0;
        if self.policy == ErrorPolicy::Abort {
            self.halted = true;
        }
    }

    fn emit(&mut self, cp: u32) {
        if self.output_codepoints >= self.max_output {
            // For a multi-byte sequence seq_start is the lead byte; for an
            // ASCII byte (no pending sequence) it is the byte itself.
            let sequence_start = if self.seq_len > 0 { self.seq_start } else { self.next_offset };
            self.errors.push(DecodeError {
                kind: ErrorKind::OutputLimitExceeded,
                offset: self.next_offset,
                sequence_start,
                byte: None,
                sequence_len: self.seq_len,
                detail: format!(
                    "decoded output exceeded the limit of {} code points",
                    self.max_output
                ),
            });
            self.truncated = true;
            self.halted = true;
            return;
        }
        self.output_codepoints += 1;
        if self.emit_output {
            // The state machine only produces valid scalar values, so the
            // encoder cannot fail here.
            let _ = encoder::encode_codepoint(cp, &mut self.output);
        }
    }
}

/// One-shot convenience: decode a complete buffer.
pub fn decode_all(input: &[u8], policy: ErrorPolicy, max_output_codepoints: u64) -> DecodeReport {
    let mut d = Decoder::with_options(policy, max_output_codepoints, true);
    d.feed(input);
    d.finish()
}

/// One-shot convenience: strict validation without producing output.
pub fn is_valid(input: &[u8]) -> bool {
    let mut d = Decoder::validator(ErrorPolicy::Abort);
    d.feed(input);
    d.finish().ok()
}
