//! Error types shared by the whole crate.

use std::fmt;

/// All fallible operations in the crate return [`Error`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Binary input is shorter than the number of bytes the header/payload
    /// declares. `(needed, available)`.
    UnexpectedEof { needed: u64, available: u64 },
    /// A length / cardinality field exceeded the limit the caller imposed.
    /// `(declared, limit)`.
    LengthLimitExceeded { declared: u64, limit: u64 },
    /// The running input-byte budget was exhausted.
    ByteLimitExceeded,
    /// The running decoded-value budget was exhausted.
    ValueLimitExceeded,
    /// A declared value cannot occur in a well-formed stream.
    /// (byte offset, explanation).
    Malformed(u64, &'static str),
    /// The magic / version preamble was not recognised.
    BadMagic,
    /// A JSON request could not be parsed or is semantically invalid.
    BadRequest(String),
    /// Base64 input contained an illegal character or bad padding.
    BadBase64(String),
    /// Underlying reader/writer failure that is not a clean EOF.
    Io(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnexpectedEof { needed, available } => write!(
                f,
                "unexpected end of input: need {needed} bytes, only {available} available"
            ),
            Error::LengthLimitExceeded { declared, limit } => write!(
                f,
                "declared length {declared} exceeds the configured limit {limit}"
            ),
            Error::ByteLimitExceeded => f.write_str("input byte limit exceeded"),
            Error::ValueLimitExceeded => f.write_str("decoded value limit exceeded"),
            Error::Malformed(at, why) => write!(f, "malformed stream at byte {at}: {why}"),
            Error::BadMagic => f.write_str("bad magic or unsupported version"),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
            Error::BadBase64(m) => write!(f, "bad base64: {m}"),
            Error::Io(m) => write!(f, "i/o error: {m}"),
        }
    }
}

impl std::error::Error for Error {}

/// Crate result alias.
pub type Result<T> = std::result::Result<T, Error>;
