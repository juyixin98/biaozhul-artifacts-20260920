//! Error types shared across the engine, storage and HTTP layers.

use std::fmt;
use std::io;

/// All fallible operations in the crate return this error.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure (disk, injected fault, unexpected EOF, ...).
    Io(io::Error),
    /// A persisted run file fails its structural or checksum verification.
    Corrupt(String),
    /// Caller supplied an invalid configuration or HTTP parameter.
    BadRequest(String),
    /// A job cannot be resumed with a different configuration than recorded.
    ConflictingJob(String),
    /// Requested entity (job, input file, ...) does not exist.
    NotFound(String),
}

impl Error {
    pub fn corrupt(msg: impl Into<String>) -> Self {
        Error::Corrupt(msg.into())
    }
    pub fn bad(msg: impl Into<String>) -> Self {
        Error::BadRequest(msg.into())
    }
    pub fn conflict(msg: impl Into<String>) -> Self {
        Error::ConflictingJob(msg.into())
    }
    pub fn not_found(msg: impl Into<String>) -> Self {
        Error::NotFound(msg.into())
    }

    /// HTTP status code used by the validation server.
    pub fn http_status(&self) -> u16 {
        match self {
            Error::BadRequest(_) => 400,
            Error::ConflictingJob(_) => 409,
            Error::NotFound(_) => 404,
            Error::Corrupt(_) | Error::Io(_) => 500,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Corrupt(m) => write!(f, "corrupt segment: {m}"),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
            Error::ConflictingJob(m) => write!(f, "job conflict: {m}"),
            Error::NotFound(m) => write!(f, "not found: {m}"),
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

pub type Result<T> = std::result::Result<T, Error>;
