//! Error types for the frame layer and the multiplexing layer.

use std::fmt;

/// Wire error codes carried inside an `ERROR` frame payload.
///
/// Codes `0..=31` are protocol-level errors; codes `32..=63` are produced by
/// the multiplexing endpoints; application-defined codes start at 64.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum ErrorCode {
    /// Payload length exceeded the configured limit. Fatal to the connection.
    PayloadTooLarge = 1,
    /// Frame checksum (CRC-32) did not match. Fatal to the connection.
    CrcMismatch = 2,
    /// Only protocol version 1 is understood. Fatal to the connection.
    UnsupportedVersion = 3,
    /// Reserved flag bits that must be zero were set.
    UnknownFlag = 4,
    /// Command byte is not one of REQUEST/RESPONSE/CANCEL/ERROR/PING/PONG.
    UnknownCommand = 5,
    /// Frame was cut short by the stream ending (clean EOF mid-frame).
    /// Fatal only because no further framing is possible on this connection.
    UnexpectedEof = 6,
    /// A request arrived with an id already outstanding on this connection.
    DuplicateRequest = 7,
    /// Server rejected the request due to too many in-flight requests.
    ServerBusy = 8,
    /// Peer sent an ERROR/RESPONSE for a request id that is not outstanding.
    NoSuchRequest = 9,
    /// A frame that is only valid as a request (REQUEST/CANCEL) appeared on
    /// the server->client channel, or vice versa.
    InvalidDirection = 10,
    /// The endpoint is shutting down.
    Shutdown = 11,
    /// Operation explicitly cancelled by the caller (local status, never sent).
    Cancelled = 12,
    /// No answer arrived before the caller-supplied deadline (local status).
    Timeout = 13,
    /// The underlying TCP connection was lost.
    ConnectionClosed = 14,
    /// The local in-flight table is full (bounded memory limit reached).
    TooManyInflight = 15,
    /// An operation was attempted on a connection after it failed fatally.
    Poisoned = 16,
    /// Application-level error reported by the test service.
    AppError = 64,
}

impl ErrorCode {
    /// Decode a wire u16 into an [`ErrorCode`]; unknown values are preserved
    /// numerically so a newer peer's error code is not misinterpreted.
    pub fn from_wire(value: u16) -> Result<ErrorCode, u16> {
        match value {
            1 => Ok(ErrorCode::PayloadTooLarge),
            2 => Ok(ErrorCode::CrcMismatch),
            3 => Ok(ErrorCode::UnsupportedVersion),
            4 => Ok(ErrorCode::UnknownFlag),
            5 => Ok(ErrorCode::UnknownCommand),
            6 => Ok(ErrorCode::UnexpectedEof),
            7 => Ok(ErrorCode::DuplicateRequest),
            8 => Ok(ErrorCode::ServerBusy),
            9 => Ok(ErrorCode::NoSuchRequest),
            10 => Ok(ErrorCode::InvalidDirection),
            11 => Ok(ErrorCode::Shutdown),
            12 => Ok(ErrorCode::Cancelled),
            13 => Ok(ErrorCode::Timeout),
            14 => Ok(ErrorCode::ConnectionClosed),
            15 => Ok(ErrorCode::TooManyInflight),
            16 => Ok(ErrorCode::Poisoned),
            64 => Ok(ErrorCode::AppError),
            other => Err(other),
        }
    }

    pub fn as_u16(self) -> u16 {
        self as u16
    }

    /// Fatal framing errors destroy framing trust and must close the
    /// connection; other errors may be delivered per-request.
    pub fn is_fatal(self) -> bool {
        matches!(
            self,
            ErrorCode::PayloadTooLarge
                | ErrorCode::CrcMismatch
                | ErrorCode::UnsupportedVersion
                | ErrorCode::UnexpectedEof
                | ErrorCode::Poisoned
        )
    }
}

/// Errors produced by the incremental parser.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// Not enough bytes yet. This is *not* an error condition: feed more
    /// bytes and retry.
    NeedMore,
    /// Stream does not begin with the protocol magic. Fatal.
    BadMagic,
    PayloadTooLarge {
        declared: u32,
        limit: u32,
    },
    CrcMismatch {
        got: u32,
        computed: u32,
    },
    UnsupportedVersion(u8),
    UnexpectedEof,
    /// Parser entered a terminal state after a fatal error; it must not be
    /// fed again. Carries the original fatal error.
    Poisoned(Box<ParseError>),
}

impl ParseError {
    pub fn is_fatal(&self) -> bool {
        !matches!(self, ParseError::NeedMore)
    }

    pub fn code(&self) -> ErrorCode {
        match self {
            ParseError::NeedMore => unreachable!("NeedMore has no wire code"),
            // A wrong magic is indistinguishable from a corrupted stream to
            // the peer; it is logged locally and the connection is closed.
            ParseError::BadMagic => ErrorCode::CrcMismatch,
            ParseError::PayloadTooLarge { .. } => ErrorCode::PayloadTooLarge,
            ParseError::CrcMismatch { .. } => ErrorCode::CrcMismatch,
            ParseError::UnsupportedVersion(_) => ErrorCode::UnsupportedVersion,
            ParseError::UnexpectedEof => ErrorCode::UnexpectedEof,
            ParseError::Poisoned(inner) => inner.code(),
        }
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::NeedMore => write!(f, "incomplete frame: more bytes needed"),
            ParseError::BadMagic => write!(f, "bad magic: stream is not protocol v1"),
            ParseError::PayloadTooLarge { declared, limit } => write!(
                f,
                "payload too large: declared {declared} bytes, limit is {limit}"
            ),
            ParseError::CrcMismatch { got, computed } => write!(
                f,
                "crc mismatch: frame carries 0x{got:08x}, computed 0x{computed:08x}"
            ),
            ParseError::UnsupportedVersion(v) => {
                write!(f, "unsupported protocol version {v}")
            }
            ParseError::UnexpectedEof => write!(f, "stream ended in the middle of a frame"),
            ParseError::Poisoned(inner) => write!(f, "parser poisoned by fatal error: {inner}"),
        }
    }
}

impl std::error::Error for ParseError {}

/// Errors produced by the multiplexing client (`Connection::call` etc.).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpcError {
    pub code: ErrorCode,
    pub message: String,
}

impl RpcError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        RpcError {
            code,
            message: message.into(),
        }
    }

    pub fn fatal(&self) -> bool {
        self.code.is_fatal()
    }
}

impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}: {}", self.code, self.message)
    }
}

impl std::error::Error for RpcError {}

impl From<ParseError> for RpcError {
    fn from(e: ParseError) -> Self {
        RpcError::new(e.code(), e.to_string())
    }
}

/// Encode an error into an ERROR-frame payload: `code:u16-be` then UTF-8 text.
pub fn encode_error_payload(code: ErrorCode, message: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + message.len());
    out.extend_from_slice(&code.as_u16().to_be_bytes());
    out.extend_from_slice(message.as_bytes());
    out
}

/// Decode an ERROR-frame payload. A malformed payload degrades to
/// `UnknownCommand`-style generic text rather than failing framing.
pub fn decode_error_payload(payload: &[u8]) -> (Result<ErrorCode, u16>, String) {
    if payload.len() < 2 {
        return (
            Err(0),
            format!("<malformed error payload: {} bytes>", payload.len()),
        );
    }
    let code = u16::from_be_bytes([payload[0], payload[1]]);
    let message = String::from_utf8_lossy(&payload[2..]).into_owned();
    (ErrorCode::from_wire(code), message)
}
