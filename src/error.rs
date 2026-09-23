use std::fmt;

/// Crate-wide error type.
#[derive(Debug)]
pub enum Error {
    /// Propagated operating-system I/O failure.
    Io(std::io::Error),
    /// The on-disk bytes violate the format (checksum, ordering, bounds, …).
    Corruption(String),
    /// The caller violated an API precondition (unsorted keys, bad options, …).
    InvalidArgument(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn corruption(msg: impl Into<String>) -> Self {
        Error::Corruption(msg.into())
    }
    pub fn invalid_argument(msg: impl Into<String>) -> Self {
        Error::InvalidArgument(msg.into())
    }

    pub(crate) fn io_write(msg: impl Into<String>) -> Self {
        Error::Io(std::io::Error::new(
            std::io::ErrorKind::WriteZero,
            msg.into(),
        ))
    }

    pub(crate) fn io_read(msg: impl Into<String>) -> Self {
        Error::Io(std::io::Error::new(
            std::io::ErrorKind::UnexpectedEof,
            msg.into(),
        ))
    }

    /// Stable short kind string, used by the HTTP layer and reports.
    pub fn kind(&self) -> &'static str {
        match self {
            Error::Io(_) => "io",
            Error::Corruption(_) => "corruption",
            Error::InvalidArgument(_) => "invalid_argument",
        }
    }

    /// `true` when the underlying I/O error is "no such file or directory".
    pub fn is_not_found(&self) -> bool {
        matches!(
            self,
            Error::Io(e) if e.kind() == std::io::ErrorKind::NotFound
        )
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Corruption(m) => write!(f, "corruption: {m}"),
            Error::InvalidArgument(m) => write!(f, "invalid argument: {m}"),
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
