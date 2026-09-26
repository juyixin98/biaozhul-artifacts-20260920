//! Error type shared by the codec and the control entry.

use std::fmt;

/// All failure modes of the codec. Decoding untrusted input must always
/// produce one of these errors instead of panicking or mis-allocating.
#[derive(Debug)]
pub enum Error {
    /// Input ended before a complete structure could be read.
    UnexpectedEof,
    /// Magic bytes did not match `BPB1`.
    InvalidMagic,
    /// Stream version is not supported by this implementation.
    UnsupportedVersion(u16),
    /// Flags field is not supported (only little-endian flag 0 exists).
    UnsupportedFlags(u16),
    /// Bit width outside the legal range 0..=64.
    InvalidBitWidth(u32),
    /// Block size outside the legal range.
    InvalidBlockSize(u32),
    /// A structural invariant of the format was violated.
    Corrupt(&'static str),
    /// A configured resource limit was exceeded.
    LimitExceeded(&'static str),
    /// Extra bytes after the end of the stream.
    TrailingBytes,
    /// JSON request could not be parsed or is missing fields.
    BadRequest(String),
    /// Base64 payload could not be decoded.
    BadBase64(String),
    /// I/O failure in the control entry.
    Io(std::io::Error),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnexpectedEof => write!(f, "unexpected end of input"),
            Error::InvalidMagic => write!(f, "invalid magic bytes"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported version {v}"),
            Error::UnsupportedFlags(fl) => write!(f, "unsupported flags {fl}"),
            Error::InvalidBitWidth(w) => write!(f, "invalid bit width {w} (must be 0..=64)"),
            Error::InvalidBlockSize(s) => write!(f, "invalid block size {s}"),
            Error::Corrupt(what) => write!(f, "corrupt stream: {what}"),
            Error::LimitExceeded(what) => write!(f, "limit exceeded: {what}"),
            Error::TrailingBytes => write!(f, "trailing bytes after stream end"),
            Error::BadRequest(msg) => write!(f, "bad request: {msg}"),
            Error::BadBase64(msg) => write!(f, "bad base64: {msg}"),
            Error::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
