//! Error types for incremental UTF-8 decoding and encoding.

use core::fmt;

/// Classification of a decoding failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// A byte that cannot start a UTF-8 sequence: a stray continuation
    /// byte (0x80..=0xBF) with no lead byte, or 0xF8..=0xFF which are
    /// not valid anywhere in UTF-8.
    InvalidLeadByte,
    /// The sequence encodes a value in more bytes than necessary
    /// (lead bytes 0xC0/0xC1, or a restricted first continuation byte
    /// below its legal minimum, e.g. `E0 80`, `F0 80`).
    OverlongEncoding,
    /// A byte outside 0x80..=0xBF appeared where a continuation byte
    /// was required.
    InvalidContinuationByte,
    /// The sequence encodes a UTF-16 surrogate half (U+D800..=U+DFFF),
    /// e.g. `ED A0 80`.
    SurrogateCodePoint,
    /// The sequence encodes a value above U+10FFFF
    /// (lead bytes 0xF5..=0xF7, or `F4 90..`).
    CodePointOutOfRange,
    /// The input ended while a multi-byte sequence was still pending.
    IncompleteSequence,
    /// Decoded output exceeded the configured `max_output_codepoints`.
    OutputLimitExceeded,
}

impl ErrorKind {
    /// Stable machine-readable token used in JSON responses.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InvalidLeadByte => "invalid_lead_byte",
            ErrorKind::OverlongEncoding => "overlong_encoding",
            ErrorKind::InvalidContinuationByte => "invalid_continuation_byte",
            ErrorKind::SurrogateCodePoint => "surrogate_code_point",
            ErrorKind::CodePointOutOfRange => "code_point_out_of_range",
            ErrorKind::IncompleteSequence => "incomplete_sequence",
            ErrorKind::OutputLimitExceeded => "output_limit_exceeded",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A single decoding failure, with absolute stream offsets in bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError {
    pub kind: ErrorKind,
    /// Absolute stream offset of the byte that triggered the error.
    /// For [`ErrorKind::IncompleteSequence`] this is the offset of the
    /// lead byte of the unfinished sequence.
    pub offset: u64,
    /// Absolute stream offset of the lead byte of the offending sequence.
    pub sequence_start: u64,
    /// The offending byte, when a single byte caused the error.
    pub byte: Option<u8>,
    /// Number of bytes of the sequence that had been consumed,
    /// including the lead byte.
    pub sequence_len: u8,
    /// Human-readable explanation.
    pub detail: String,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte offset {}: {}", self.kind, self.offset, self.detail)
    }
}

impl std::error::Error for DecodeError {}

/// Classification of an encoding failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeErrorKind {
    /// The code point is a UTF-16 surrogate half (U+D800..=U+DFFF).
    SurrogateCodePoint,
    /// The code point is above U+10FFFF.
    CodePointOutOfRange,
}

impl EncodeErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            EncodeErrorKind::SurrogateCodePoint => "surrogate_code_point",
            EncodeErrorKind::CodePointOutOfRange => "code_point_out_of_range",
        }
    }
}

/// A failure to encode one code point.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncodeError {
    pub kind: EncodeErrorKind,
    pub codepoint: u32,
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: U+{:04X}", self.kind.as_str(), self.codepoint)
    }
}

impl std::error::Error for EncodeError {}
