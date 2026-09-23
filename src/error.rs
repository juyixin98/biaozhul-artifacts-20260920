//! Error types shared by the service, storage and verification layers.

use std::io;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// All fallible operations surface one of these errors.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// A request referenced a version that was never published.
    #[error("unknown version: {0}")]
    UnknownVersion(u64),

    /// The supplied operation / proof payload is malformed.
    #[error("bad request: {0}")]
    BadRequest(String),

    /// Persistence failure (RocksDB I/O, corrupt data, ...).
    #[error("storage error: {0}")]
    Storage(String),

    /// Wrapped IO error.
    #[error("io error: {0}")]
    Io(#[from] io::Error),
}

impl Error {
    pub(crate) fn bad(msg: impl Into<String>) -> Self {
        Error::BadRequest(msg.into())
    }
}

impl From<rocksdb::Error> for Error {
    fn from(e: rocksdb::Error) -> Self {
        Error::Storage(e.to_string())
    }
}
