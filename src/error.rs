//! Error types for the ecstripe library.
//!
//! Every failure mode of the streaming encoder/decoder is represented here so
//! that callers (including the JSON control entry point) can distinguish
//! recoverable erasure-coding limitations from I/O or framing failures.

use std::fmt;
use std::io;

/// Crate-wide error type.
#[derive(Debug)]
#[non_exhaustive]
pub enum Error {
    /// Wrapper around an underlying I/O failure.
    Io(io::Error),
    /// Caller supplied invalid coding parameters or limits.
    InvalidParams(String),
    /// Container does not start with the ecstripe magic bytes.
    BadMagic,
    /// Container version is not supported by this build.
    UnsupportedVersion(u8),
    /// Header JSON is missing, malformed, or inconsistent.
    BadHeader(String),
    /// Record framing is invalid (unknown shard id, duplicate chunk, gap, ...).
    BadRecord(String),
    /// A present chunk has a length inconsistent with the stripe layout.
    ///
    /// This is the "length mismatch" limitation: all chunks of a full stripe
    /// must be exactly `stripe_size` bytes, and final-stripe chunks must match
    /// the lengths implied by the declared total length.
    LengthMismatch {
        stripe: u32,
        shard: u8,
        got: u64,
        expected: u64,
    },
    /// Fewer than `data_shards` distinct chunks are available for a stripe.
    ///
    /// Recovery is only promised when the number of missing shards does not
    /// exceed the number of parity shards.
    NotEnoughShards {
        stripe: u32,
        present: usize,
        needed: usize,
    },
    /// The coding sub-matrix was singular despite the MDS construction.
    /// This indicates an internal bug rather than a data condition.
    SingularMatrix,
    /// The byte stream ended before the container's explicit end-of-data
    /// record was seen (truncated container / lost tail).
    UnexpectedEos { stripe: u32 },
    /// Decoded output would exceed the configured output-length budget.
    OutputLimitExceeded { limit: u64, attempted: u64 },
    /// A required allocation would exceed the configured memory budget.
    MemoryLimitExceeded { requested: u64, limit: u64 },
    /// An externally supplied integrity hash does not match the decoded data.
    ///
    /// Reed-Solomon decoding cannot detect silent corruption by itself:
    /// corrupted chunks decode to equally-valid but wrong bytes. Detection
    /// requires an external checksum such as this SHA-256.
    HashMismatch { expected: String, actual: String },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "I/O error: {e}"),
            Error::InvalidParams(s) => write!(f, "invalid parameters: {s}"),
            Error::BadMagic => write!(f, "bad magic: not an ecstripe container"),
            Error::UnsupportedVersion(v) => {
                write!(f, "unsupported container format version: {v}")
            }
            Error::BadHeader(s) => write!(f, "bad header: {s}"),
            Error::BadRecord(s) => write!(f, "bad record framing: {s}"),
            Error::LengthMismatch {
                stripe,
                shard,
                got,
                expected,
            } => write!(
                f,
                "length mismatch in stripe {stripe} shard {shard}: got {got} bytes, expected {expected}"
            ),
            Error::NotEnoughShards {
                stripe,
                present,
                needed,
            } => write!(
                f,
                "not enough shards for stripe {stripe}: {present} present, {needed} needed \
                 (too many shards missing; recovery requires at least data_shards survivors)"
            ),
            Error::SingularMatrix => write!(f, "singular coding matrix (internal error)"),
            Error::UnexpectedEos { stripe } => {
                write!(f, "unexpected end of stream while reading stripe {stripe}")
            }
            Error::OutputLimitExceeded { limit, attempted } => write!(
                f,
                "decoded output length {attempted} would exceed limit of {limit} bytes"
            ),
            Error::MemoryLimitExceeded { requested, limit } => write!(
                f,
                "required working memory of {requested} bytes exceeds limit of {limit} bytes"
            ),
            Error::HashMismatch { expected, actual } => write!(
                f,
                "external integrity check failed: decoded data SHA-256 is {actual}, expected {expected}"
            ),
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

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::BadHeader(e.to_string())
    }
}

/// Convenience alias.
pub type Result<T> = std::result::Result<T, Error>;
