use std::fmt;

/// Errors produced by the codec and the JSON control entry.
#[derive(Debug)]
pub enum Error {
    /// Input ended before a complete structure could be read.
    Truncated,
    /// Stream does not start with the expected magic bytes.
    BadMagic,
    /// Stream version is not supported by this implementation.
    UnsupportedVersion(u8),
    /// Reserved header fields must be zero.
    InvalidFlags,
    /// A configured resource limit was exceeded.
    LimitExceeded(&'static str),
    /// Structurally invalid or inconsistent stream data.
    InvalidData(String),
    /// Invalid base64 input.
    Base64(String),
    /// Invalid JSON request.
    Json(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Truncated => write!(f, "input truncated"),
            Error::BadMagic => write!(f, "bad magic bytes"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported version {v}"),
            Error::InvalidFlags => write!(f, "reserved header fields must be zero"),
            Error::LimitExceeded(what) => write!(f, "limit exceeded: {what}"),
            Error::InvalidData(msg) => write!(f, "invalid data: {msg}"),
            Error::Base64(msg) => write!(f, "invalid base64: {msg}"),
            Error::Json(msg) => write!(f, "invalid json: {msg}"),
        }
    }
}

impl std::error::Error for Error {}
