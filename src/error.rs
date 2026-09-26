//! Error types for the ecstripe codec.

use std::fmt;

/// All recoverable failures of the codec and the JSON control entry.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure while reading or writing streams.
    Io(std::io::Error),
    /// Encode/decode parameters violate the documented limits.
    InvalidParams(String),
    /// The stream does not start with the `ECSR` magic.
    BadMagic,
    /// The container version byte is not supported by this build.
    UnsupportedVersion(u8),
    /// The container body is inconsistent (bad block length, truncation,
    /// trailing bytes, ...).
    CorruptFormat(String),
    /// A block length prefix does not match the header's stripe size.
    LengthMismatch { expected: u32, actual: u32 },
    /// More shards are marked missing than there are parity shards.
    TooManyErasures { missing: usize, parity: usize },
    /// A shard index in `missing_shards` is out of range or duplicated.
    InvalidShardIndex(u32),
    /// The decoded output would exceed the caller-supplied limit.
    OutputLimitExceeded { needed: u64, limit: u64 },
    /// The decoding matrix is singular (must not happen for valid
    /// parameters; indicates silent corruption of shard indices).
    SingularMatrix,
    /// The optional external SHA-256 check of the decoded output failed.
    ChecksumMismatch { expected: String, actual: String },
    /// The JSON control request is malformed.
    BadRequest(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::InvalidParams(m) => write!(f, "invalid parameters: {m}"),
            Error::BadMagic => write!(f, "bad magic: not an ECSR container"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported container version: {v}"),
            Error::CorruptFormat(m) => write!(f, "corrupt container: {m}"),
            Error::LengthMismatch { expected, actual } => write!(
                f,
                "block length mismatch: header stripe_size={expected}, block prefix={actual}"
            ),
            Error::TooManyErasures { missing, parity } => write!(
                f,
                "too many missing shards: {missing} missing but only {parity} parity shards"
            ),
            Error::InvalidShardIndex(i) => write!(f, "invalid shard index: {i}"),
            Error::OutputLimitExceeded { needed, limit } => write!(
                f,
                "decoded output of {needed} bytes exceeds limit of {limit} bytes"
            ),
            Error::SingularMatrix => write!(f, "decoding matrix is singular"),
            Error::ChecksumMismatch { expected, actual } => write!(
                f,
                "sha256 mismatch: expected {expected}, decoded to {actual}"
            ),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
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
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
