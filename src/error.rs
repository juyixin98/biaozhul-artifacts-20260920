//! Unified error type for the codec and the JSON control entry.

use std::fmt;

#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    /// Magic bytes at the start of the stream do not match `CHF1`.
    BadMagic,
    /// Unexpected end of stream while reading the header or the bitstream.
    Truncated,
    /// Kraft sum of the code-length table exceeds 1 (table is oversubscribed).
    Oversubscribed,
    /// Kraft sum is below 1 and the active policy is `reject`.
    IncompleteTable,
    /// A code length of 0 or greater than `MAX_CODE_LEN` appeared in the table.
    InvalidCodeLength(u8),
    /// The same symbol appeared twice in the header table.
    DuplicateSymbol(u8),
    /// Header declares data but the code table is empty.
    EmptyTableWithData,
    /// Bit sequence matched no code (possible with an incomplete table under
    /// the `permit` policy).
    UndefinedCode,
    /// Header-declared uncompressed size exceeds the configured output limit.
    OutputTooLarge { needed: u64, limit: u64 },
    /// Input exceeds the configured input limit.
    InputLimitExceeded { limit: u64 },
    /// Non-zero padding bits or extra bytes after the final symbol.
    TrailingData,
    /// JSON request could not be parsed.
    Json(String),
    /// JSON request is well-formed but semantically invalid.
    BadRequest(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::BadMagic => write!(f, "bad magic: not a CHF1 stream"),
            Error::Truncated => write!(f, "truncated stream: unexpected end of input"),
            Error::Oversubscribed => {
                write!(f, "oversubscribed code table: kraft sum exceeds 1")
            }
            Error::IncompleteTable => {
                write!(f, "incomplete code table rejected by policy")
            }
            Error::InvalidCodeLength(l) => write!(f, "invalid code length {l}"),
            Error::DuplicateSymbol(s) => write!(f, "duplicate symbol {s} in code table"),
            Error::EmptyTableWithData => {
                write!(f, "header declares data but code table is empty")
            }
            Error::UndefinedCode => write!(f, "bit sequence matches no code in table"),
            Error::OutputTooLarge { needed, limit } => write!(
                f,
                "declared output size {needed} bytes exceeds limit {limit} bytes"
            ),
            Error::InputLimitExceeded { limit } => {
                write!(f, "input exceeds limit of {limit} bytes")
            }
            Error::TrailingData => {
                write!(f, "non-zero padding or trailing bytes after final symbol")
            }
            Error::Json(msg) => write!(f, "json parse error: {msg}"),
            Error::BadRequest(msg) => write!(f, "bad request: {msg}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

/// Map an io error to `Truncated` when it is an unexpected EOF, else pass through.
pub(crate) fn map_eof(e: std::io::Error) -> Error {
    if e.kind() == std::io::ErrorKind::UnexpectedEof {
        Error::Truncated
    } else {
        Error::Io(e)
    }
}
