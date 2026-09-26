//! Error types for the bschema codec.
//!
//! Every failure mode the library can produce is enumerated here so that
//! callers (and the CLI) can distinguish truncation, schema rejection,
//! incompatible type evolution and resource-limit violations.

use std::fmt;
use std::io;
use std::str::Utf8Error;
use std::string::FromUtf8Error;

/// Crate-wide result alias.
pub type Result<T> = std::result::Result<T, Error>;

/// All errors returned by the codec.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure (excluding clean truncation, which is [`Error::Wire`]).
    Io(io::Error),
    /// Malformed JSON control document (schema or message).
    Json(String),
    /// Schema definition is invalid in itself.
    Schema(String),
    /// Binary input does not conform to the BSE1 wire format.
    Wire(String),
    /// A JSON/message value does not match its declared schema type.
    InvalidValue(String),
    /// A field number known to the schema was seen with an incompatible wire
    /// type (an incompatible type evolution).
    TypeMismatch {
        number: u32,
        name: String,
        expected: String,
        found: String,
    },
    /// A `required` field was absent.
    MissingRequired { number: u32, name: String },
    /// A configured memory / output-size limit was exceeded.
    LimitExceeded(String),
    /// A `string` field contained invalid UTF-8 on the wire.
    Utf8(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Json(m) => write!(f, "invalid JSON: {m}"),
            Error::Schema(m) => write!(f, "invalid schema: {m}"),
            Error::Wire(m) => write!(f, "corrupt wire data: {m}"),
            Error::InvalidValue(m) => write!(f, "invalid value: {m}"),
            Error::TypeMismatch {
                number,
                name,
                expected,
                found,
            } => write!(
                f,
                "incompatible type for field {number} ({name}): expected wire type {expected}, found {found}"
            ),
            Error::MissingRequired { number, name } => {
                write!(f, "required field {number} ({name}) is missing")
            }
            Error::LimitExceeded(m) => write!(f, "limit exceeded: {m}"),
            Error::Utf8(m) => write!(f, "invalid UTF-8: {m}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<FromUtf8Error> for Error {
    fn from(e: FromUtf8Error) -> Self {
        Error::Utf8(e.to_string())
    }
}

impl From<Utf8Error> for Error {
    fn from(e: Utf8Error) -> Self {
        Error::Utf8(e.to_string())
    }
}
