//! Error types shared by the engine, the I/O layer and the HTTP server.

use std::io;

/// Library result alias.
pub type Result<T> = std::result::Result<T, Error>;

/// Errors raised by the storage engine.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure, optionally caused by an injected fault.
    Io(io::Error),
    /// A disk file was corrupt and could not be replayed.
    Corrupt(String),
    /// The key does not exist at the read snapshot.
    NotFound,
    /// First-writer-wins conflict: another transaction committed one of the
    /// keys this transaction wrote after this transaction started.
    /// Carries the first conflicting key.
    Conflict(Vec<u8>),
    /// The transaction/snapshot id is unknown or already finished.
    UnknownTx(u64),
    /// The snapshot id is unknown or already released.
    UnknownSnapshot(u64),
    /// Request body was not valid for the endpoint.
    BadRequest(String),
    /// A fault was configured to fire on the next matching I/O operation.
    Injected(String),
}

impl Error {
    pub fn corrupt(msg: impl Into<String>) -> Self {
        Error::Corrupt(msg.into())
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Corrupt(m) => write!(f, "corrupt file: {m}"),
            Error::NotFound => write!(f, "key not found"),
            Error::Conflict(k) => write!(
                f,
                "write-write conflict on key {:?}",
                String::from_utf8_lossy(k)
            ),
            Error::UnknownTx(id) => write!(f, "unknown or finished transaction {id}"),
            Error::UnknownSnapshot(id) => write!(f, "unknown or released snapshot {id}"),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
            Error::Injected(m) => write!(f, "injected fault: {m}"),
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
