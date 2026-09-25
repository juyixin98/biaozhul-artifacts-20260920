//! Demo RPC payload encoding (the *application* layer carried inside frames).
//!
//! This is intentionally tiny and self-describing: it exists so the mux
//! client/server have real request/response types to interleave, and to show
//! that application-level errors travel inside well-formed frames (they never
//! poison the byte parser).
//!
//! Request:
//! ```text
//!  method: u16 BE, then method-specific arguments
//! ```
//! Response:
//! ```text
//!  code:   u16 BE  (0 = OK)
//!  body:   code-specific bytes
//! ```

/// Application result codes carried in a response frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum AppCode {
    Ok = 0,
    UnknownMethod = 1,
    BadArgs = 2,
    /// Server cancelled the work because the client sent CANCEL (or the
    /// requester went away).
    Cancelled = 3,
    /// Server refused because too many requests are in flight on this conn.
    TooManyInFlight = 4,
    /// Generic server-side failure.
    Internal = 5,
}

impl AppCode {
    pub fn from_u16(v: u16) -> AppCode {
        match v {
            0 => AppCode::Ok,
            1 => AppCode::UnknownMethod,
            2 => AppCode::BadArgs,
            3 => AppCode::Cancelled,
            4 => AppCode::TooManyInFlight,
            _ => AppCode::Internal,
        }
    }

    pub fn as_u16(self) -> u16 {
        self as u16
    }
}

/// Methods understood by the demo server.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum Method {
    /// Echo body straight back. Args: arbitrary bytes.
    Echo = 1,
    /// Reply after `delay_ms` (u32 BE) — used to force out-of-order replies.
    /// Remaining bytes are echoed.
    Slow = 2,
    /// Add two i64 BE values; body is the i64 sum.
    Add = 3,
    /// Empty args/body; body = stats snapshot (see [`crate::server::Stats`]).
    Stats = 4,
}

impl Method {
    pub fn from_u16(v: u16) -> Option<Method> {
        match v {
            1 => Some(Method::Echo),
            2 => Some(Method::Slow),
            3 => Some(Method::Add),
            4 => Some(Method::Stats),
            _ => None,
        }
    }

    pub fn as_u16(self) -> u16 {
        self as u16
    }
}

/// Encode a request payload.
pub fn encode_request(method: Method, args: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + args.len());
    out.extend_from_slice(&method.as_u16().to_be_bytes());
    out.extend_from_slice(args);
    out
}

/// Decode just the method (subset parse of the request payload).
pub fn decode_method(payload: &[u8]) -> Result<Method, AppCode> {
    let m = read_u16(payload, 0).ok_or(AppCode::BadArgs)?;
    Method::from_u16(m).ok_or(AppCode::UnknownMethod)
}

/// Arguments slice (everything after the method u16).
pub fn request_args(payload: &[u8]) -> Result<&[u8], AppCode> {
    payload.get(2..).ok_or(AppCode::BadArgs)
}

/// Encode a success response.
pub fn encode_ok(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + body.len());
    out.extend_from_slice(&AppCode::Ok.as_u16().to_be_bytes());
    out.extend_from_slice(body);
    out
}

/// Encode an application error response (empty body, optional UTF-8 message).
pub fn encode_err(code: AppCode, message: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(2 + message.len());
    out.extend_from_slice(&code.as_u16().to_be_bytes());
    out.extend_from_slice(message.as_bytes());
    out
}

/// Split a response payload into (code, body).
pub fn decode_response(payload: &[u8]) -> Result<(AppCode, &[u8]), AppCode> {
    let code = AppCode::from_u16(read_u16(payload, 0).ok_or(AppCode::BadArgs)?);
    let body = payload.get(2..).unwrap_or(&[]);
    Ok((code, body))
}

// small BE readers ----------------------------------------------------------------

pub fn read_u16(b: &[u8], at: usize) -> Option<u16> {
    let s = b.get(at..at + 2)?;
    Some(u16::from_be_bytes([s[0], s[1]]))
}

pub fn read_u32(b: &[u8], at: usize) -> Option<u32> {
    let s = b.get(at..at + 4)?;
    Some(u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
}

pub fn read_i64(b: &[u8], at: usize) -> Option<i64> {
    let s = b.get(at..at + 8)?;
    let mut a = [0u8; 8];
    a.copy_from_slice(s);
    Some(i64::from_be_bytes(a))
}
