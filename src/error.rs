//! Error type and configurable resource limits.

use std::fmt;

/// Resource limits applied while encoding and decoding.
///
/// Every bound is checked *while streaming*; exceeding any one aborts the
/// operation with [`Error::LimitExceeded`] rather than allocating unbounded
/// memory.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Maximum bytes buffered for a single segment's dictionary strings.
    pub max_dict_bytes: u64,
    /// Maximum distinct entries in a single segment dictionary (also bounds
    /// the merged global dictionary and the sum of per-segment dictionary
    /// sizes accepted by a merge).
    pub max_dict_entries: u64,
    /// Maximum rows in a single segment (also bounds total merged rows).
    pub max_rows: u64,
    /// Maximum length in bytes of one dictionary string.
    pub max_string_bytes: u64,
    /// Maximum bytes of decoded string content emitted (sum across all
    /// non-NULL rows). Allows callers to bound decode output size.
    pub max_decoded_string_bytes: u64,
    /// Maximum number of segments accepted by a merge/decode call.
    pub max_segments: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_dict_bytes: 256 * 1024 * 1024,
            max_dict_entries: 10_000_000,
            max_rows: 10_000_000,
            max_string_bytes: 16 * 1024 * 1024,
            max_decoded_string_bytes: 512 * 1024 * 1024,
            max_segments: 10_000,
        }
    }
}

impl Limits {
    /// Extremely tight limits, mainly used by tests of the limit machinery.
    pub fn tight() -> Self {
        Self {
            max_dict_bytes: 128,
            max_dict_entries: 4,
            max_rows: 64,
            max_string_bytes: 16,
            max_decoded_string_bytes: 128,
            max_segments: 8,
        }
    }
}

/// Errors produced by encode/decode/merge operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Input bytes ended before a complete value could be read.
    Truncated(String),
    /// A field had a value the format forbids (bad tag, bad version, ...).
    Invalid(String),
    /// A configured [`Limits`] threshold was exceeded.
    LimitExceeded(String),
    /// An index/ID referenced something that does not exist.
    BadReference(String),
    /// Underlying I/O failure.
    Io(String),
}

impl Error {
    pub(crate) fn truncated(what: impl Into<String>) -> Self {
        Error::Truncated(what.into())
    }
    pub(crate) fn invalid(what: impl Into<String>) -> Self {
        Error::Invalid(what.into())
    }
    pub(crate) fn limit(what: impl Into<String>) -> Self {
        Error::LimitExceeded(what.into())
    }
    pub(crate) fn bad_ref(what: impl Into<String>) -> Self {
        Error::BadReference(what.into())
    }
    pub fn io(err: std::io::Error) -> Self {
        Error::Io(err.to_string())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Truncated(s) => write!(f, "truncated input while reading {s}"),
            Error::Invalid(s) => write!(f, "invalid data: {s}"),
            Error::LimitExceeded(s) => write!(f, "resource limit exceeded: {s}"),
            Error::BadReference(s) => write!(f, "dangling reference: {s}"),
            Error::Io(s) => write!(f, "i/o error: {s}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e.to_string())
    }
}

impl From<std::string::FromUtf8Error> for Error {
    fn from(e: std::string::FromUtf8Error) -> Self {
        Error::Invalid(format!("dictionary string is not valid UTF-8: {e}"))
    }
}

/// Crate result alias.
pub type Result<T> = std::result::Result<T, Error>;
