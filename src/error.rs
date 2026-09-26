//! Error types shared by the codec, schema layer and JSON control entry point.

use std::fmt;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// All errors produced by the library.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// A varint exceeded the 10-byte / 64-bit limit, or was truncated.
    InvalidVarint,
    /// Input ended before a value was complete. `needed` is the number of
    /// additional bytes that would have been required when known.
    UnexpectedEof { needed: Option<usize> },
    /// A length-delimited value declared more bytes than remained in input.
    LengthOutOfBounds { declared: u64, remaining: u64 },
    /// A declared length exceeded the decoder's configured budget.
    LengthExceedsLimit { declared: u64, limit: u64 },
    /// Total decoded output (recursive byte accounting) exceeded the budget.
    OutputLimitExceeded { limit: u64 },
    /// Recursion went deeper than the configured nesting limit.
    NestingTooDeep { limit: u32 },
    /// A field number was zero (field numbers start at 1).
    InvalidFieldNumber(u64),
    /// A wire-type byte was unknown to this decoder.
    UnknownWireType(u8),
    /// Bytes could not be interpreted as the requested scalar type
    /// (for example a non-UTF-8 string or a bad float length).
    MalformedValue(String),
    /// A JSON control document was syntactically invalid.
    Json(String),
    /// A schema document was invalid.
    Schema(String),
    /// A message value did not match its schema.
    TypeMismatch(String),
    /// A `required` field was absent.
    MissingField(String),
    /// Reading data written under one schema with another schema was rejected
    /// because the two are not wire-compatible for the field.
    IncompatibleEvolution { field: u32, reason: String },
    /// A schema or message used an unsupported value (duplicate field, ...).
    InvalidInput(String),
    /// Base64 input (for `bytes_bin`) was invalid.
    InvalidBase64(String),
    /// An I/O error from a stream.
    Io(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidVarint => write!(f, "invalid or overflowed varint"),
            Error::UnexpectedEof { needed } => match needed {
                Some(n) => write!(f, "unexpected end of input; {n} more byte(s) needed"),
                None => write!(f, "unexpected end of input"),
            },
            Error::LengthOutOfBounds {
                declared,
                remaining,
            } => write!(
                f,
                "length-delimited value declares {declared} bytes but only {remaining} remain"
            ),
            Error::LengthExceedsLimit { declared, limit } => write!(
                f,
                "declared length {declared} exceeds decoder length limit {limit}"
            ),
            Error::OutputLimitExceeded { limit } => {
                write!(
                    f,
                    "decoded output exceeds the configured limit of {limit} bytes"
                )
            }
            Error::NestingTooDeep { limit } => {
                write!(f, "nesting depth exceeds the configured limit of {limit}")
            }
            Error::InvalidFieldNumber(n) => {
                write!(f, "field number {n} is invalid (must be 1..=2^29-1)")
            }
            Error::UnknownWireType(t) => write!(f, "unknown wire type {t}"),
            Error::MalformedValue(s) => write!(f, "malformed value: {s}"),
            Error::Json(s) => write!(f, "invalid JSON: {s}"),
            Error::Schema(s) => write!(f, "invalid schema: {s}"),
            Error::TypeMismatch(s) => write!(f, "type mismatch: {s}"),
            Error::MissingField(s) => write!(f, "missing required field: {s}"),
            Error::IncompatibleEvolution { field, reason } => {
                write!(
                    f,
                    "incompatible schema evolution for field {field}: {reason}"
                )
            }
            Error::InvalidInput(s) => write!(f, "invalid input: {s}"),
            Error::InvalidBase64(s) => write!(f, "invalid base64: {s}"),
            Error::Io(s) => write!(f, "I/O error: {s}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e.to_string())
    }
}
