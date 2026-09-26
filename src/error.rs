//! Error types for incremental UTF-8 decoding.
//!
//! Every error carries the *absolute* byte offset in the logical stream
//! (i.e. counted across all `feed` calls since the last reset), so callers
//! can locate corrupted content in the original byte stream even when it
//! was delivered in arbitrary chunks.

use core::fmt;

/// Classification of a decoding failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorKind {
    /// A continuation byte (`0x80..=0xBF`) appeared where a lead byte (or
    /// ASCII byte) was expected.
    UnexpectedContinuation,
    /// The byte can never start a valid UTF-8 sequence (`0xF5..=0xFF`).
    /// (`0xC0`/`0xC1` are reported as [`ErrorKind::OverlongEncoding`]
    /// because that is the only thing they could ever encode.)
    InvalidLeadByte,
    /// A continuation byte was outside the range `0x80..=0xBF` (or outside
    /// the tighter range required at its position) and no more specific
    /// classification applies.
    InvalidContinuation,
    /// The sequence encodes a code point with more bytes than necessary
    /// (e.g. `C0 80` for U+0000, `E0 80 80`, `F0 80 80 80`).
    OverlongEncoding,
    /// The sequence encodes a UTF-16 surrogate half (U+D800..=U+DFFF),
    /// which is not a valid Unicode scalar value.
    Surrogate,
    /// The sequence encodes a value above U+10FFFF.
    OutOfRange,
    /// `finish` was called while a multi-byte sequence was incomplete.
    /// `offset`/`sequence_start` point at the first byte of the dangling
    /// sequence in the original stream.
    TruncatedSequence,
    /// The configured total input byte limit was exceeded.
    InputLimitExceeded,
    /// The configured total output code point limit was exceeded.
    OutputLimitExceeded,
}

impl ErrorKind {
    /// Stable snake_case identifier used in the JSON control protocol.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::UnexpectedContinuation => "unexpected_continuation",
            ErrorKind::InvalidLeadByte => "invalid_lead_byte",
            ErrorKind::InvalidContinuation => "invalid_continuation",
            ErrorKind::OverlongEncoding => "overlong_encoding",
            ErrorKind::Surrogate => "surrogate",
            ErrorKind::OutOfRange => "out_of_range",
            ErrorKind::TruncatedSequence => "truncated_sequence",
            ErrorKind::InputLimitExceeded => "input_limit_exceeded",
            ErrorKind::OutputLimitExceeded => "output_limit_exceeded",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A single decoding failure, located in the original byte stream.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DecodeError {
    /// What went wrong.
    pub kind: ErrorKind,
    /// Absolute offset of the byte that triggered the error. For
    /// [`ErrorKind::TruncatedSequence`] this equals `sequence_start`.
    pub offset: u64,
    /// Absolute offset of the first byte of the sequence that was being
    /// decoded when the error occurred (equals `offset` for lead-byte
    /// errors).
    pub sequence_start: u64,
}

impl DecodeError {
    pub(crate) fn new(kind: ErrorKind, offset: u64, sequence_start: u64) -> Self {
        DecodeError {
            kind,
            offset,
            sequence_start,
        }
    }
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} at byte offset {} (sequence started at {})",
            self.kind, self.offset, self.sequence_start
        )
    }
}

impl std::error::Error for DecodeError {}

/// Error produced by the encoder when asked to encode a value that is not
/// a Unicode scalar value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EncodeError {
    /// The value is a UTF-16 surrogate half (U+D800..=U+DFFF).
    Surrogate(u32),
    /// The value is above U+10FFFF.
    OutOfRange(u32),
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::Surrogate(cp) => write!(f, "U+{cp:04X} is a surrogate half"),
            EncodeError::OutOfRange(cp) => write!(f, "U+{cp:X} is above U+10FFFF"),
        }
    }
}

impl std::error::Error for EncodeError {}
