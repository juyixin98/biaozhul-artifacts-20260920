//! Error types shared by the encoder, decoder and JSON entry point.

use std::fmt;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// All errors produced by the library.
#[derive(Debug)]
pub enum Error {
    /// Wrapped I/O error.
    Io(std::io::Error),
    /// Input bytes do not start with a valid `AC01` header / use unsupported
    /// parameters.
    InvalidFormat(String),
    /// The coded stream ended before a terminator could be decoded.
    UnexpectedEnd,
    /// Decoded output or encoded output exceeded the caller-supplied limit.
    OutputLimitExceeded {
        /// Configured limit in bytes.
        limit: u64,
    },
    /// Base64 input was malformed.
    InvalidBase64(String),
    /// JSON request was malformed or did not match the schema.
    InvalidJson(String),
}

impl Error {
    /// Stable machine-readable identifier used in JSON error responses.
    pub fn kind(&self) -> &'static str {
        match self {
            Error::Io(_) => "Io",
            Error::InvalidFormat(_) => "InvalidFormat",
            Error::UnexpectedEnd => "UnexpectedEnd",
            Error::OutputLimitExceeded { .. } => "OutputLimitExceeded",
            Error::InvalidBase64(_) => "InvalidBase64",
            Error::InvalidJson(_) => "InvalidJson",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::InvalidFormat(msg) => write!(f, "invalid coded format: {msg}"),
            Error::UnexpectedEnd => write!(
                f,
                "unexpected end of coded stream (missing or damaged terminator)"
            ),
            Error::OutputLimitExceeded { limit } => {
                write!(f, "output length limit exceeded ({limit} bytes)")
            }
            Error::InvalidBase64(msg) => write!(f, "invalid base64: {msg}"),
            Error::InvalidJson(msg) => write!(f, "invalid JSON request: {msg}"),
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

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        // Our internal length-limit errors are surfaced through custom Error
        // kinds; everything else is a plain I/O error.
        Error::Io(e)
    }
}

impl From<Error> for std::io::Error {
    fn from(e: Error) -> Self {
        match e {
            Error::Io(ioe) => ioe,
            other => std::io::Error::new(std::io::ErrorKind::InvalidData, other),
        }
    }
}
