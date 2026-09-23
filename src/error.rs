//! Error types shared across the crate.

use std::fmt;
use std::io;

/// Crate-wide result alias.
pub type Result<T> = std::result::Result<T, Error>;

/// All errors produced by the external-sort engine, repository, I/O layer and
/// HTTP server.
#[derive(Debug)]
pub enum Error {
    /// Wrapped OS I/O error carrying the affected path (when known).
    Io(io::Error, String),
    /// A run file fails its integrity checks (bad magic, length, CRC, ...).
    Corrupt { path: String, detail: String },
    /// A configuration value is invalid.
    Config(String),
    /// The requested memory budget is below the hard floor.
    BudgetTooSmall { budget: u64, min: u64 },
    /// A single record's *key* prefix is larger than the whole budget. Payload
    /// bytes are streamed, but the key must fit alongside bookkeeping.
    KeyTooLarge {
        seq: u64,
        needed: usize,
        budget: u64,
    },
    /// A resume run references a record count inconsistent with the manifest.
    InconsistentState(String),
    /// HTTP request could not be parsed.
    BadRequest(String),
    /// HTTP resource does not exist.
    NotFound(String),
    /// Fault injected through the [`crate::io::FaultPlan`] layer.
    Fault(String),
}

impl Error {
    pub fn io(err: io::Error, path: impl Into<String>) -> Self {
        Error::Io(err, path.into())
    }

    pub fn corrupt(path: impl Into<String>, detail: impl Into<String>) -> Self {
        Error::Corrupt {
            path: path.into(),
            detail: detail.into(),
        }
    }

    /// Short machine-style code used in HTTP status bodies.
    pub fn code(&self) -> &'static str {
        match self {
            Error::Io(..) => "io_error",
            Error::Corrupt { .. } => "corrupt_segment",
            Error::Config(_) => "bad_config",
            Error::BudgetTooSmall { .. } => "budget_too_small",
            Error::KeyTooLarge { .. } => "key_too_large",
            Error::InconsistentState(_) => "inconsistent_state",
            Error::BadRequest(_) => "bad_request",
            Error::NotFound(_) => "not_found",
            Error::Fault(_) => "fault_injected",
        }
    }

    /// HTTP status mapped for server errors.
    pub fn http_status(&self) -> u16 {
        match self {
            Error::BadRequest(_)
            | Error::Config(_)
            | Error::BudgetTooSmall { .. }
            | Error::KeyTooLarge { .. } => 400,
            Error::NotFound(_) => 404,
            _ => 500,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e, p) => write!(f, "I/O error on {p}: {e}"),
            Error::Corrupt { path, detail } => write!(f, "corrupt segment {path}: {detail}"),
            Error::Config(m) => write!(f, "bad config: {m}"),
            Error::BudgetTooSmall { budget, min } => write!(
                f,
                "memory budget {budget} bytes is below minimum {min} bytes"
            ),
            Error::KeyTooLarge { seq, needed, budget } => write!(
                f,
                "record #{seq} key prefix needs {needed} bytes, larger than the whole budget {budget}"
            ),
            Error::InconsistentState(m) => write!(f, "inconsistent state: {m}"),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
            Error::NotFound(m) => write!(f, "not found: {m}"),
            Error::Fault(m) => write!(f, "injected fault: {m}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e, _) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e, String::new())
    }
}
