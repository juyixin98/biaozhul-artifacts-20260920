//! The demo/test service carried inside REQUEST/RESPONSE payloads.
//!
//! Application payload format (big-endian):
//!
//! ```text
//! request:  op:u8  args...
//! response: status:u8  body...
//! ```
//!
//! Status 0 = OK. Non-zero is an application error with a UTF-8 message body.

use crate::error::ErrorCode;

pub const OP_ECHO: u8 = 1;
pub const OP_UPPER: u8 = 2;
pub const OP_SLOW: u8 = 3;
pub const OP_ADD: u8 = 4;
pub const OP_FAIL: u8 = 5;

pub const STATUS_OK: u8 = 0;
pub const STATUS_APP_ERROR: u8 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceRequest {
    /// Echo the rest of the payload back verbatim.
    Echo(Vec<u8>),
    /// ASCII-uppercase the rest of the payload.
    Upper(Vec<u8>),
    /// Sleep `delay_ms`, then echo the trailing bytes. Lets tests exercise
    /// timeouts, late responses and cancellation races.
    Slow { delay_ms: u32, body: Vec<u8> },
    /// Add two u64s (exactly 16 bytes of args).
    Add(u64, u64),
    /// Always reply with an application error.
    Fail,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceResponse {
    Ok(Vec<u8>),
    AppError(String),
}

impl ServiceRequest {
    pub fn decode(payload: &[u8]) -> Result<ServiceRequest, String> {
        let (&op, rest) = payload
            .split_first()
            .ok_or_else(|| "empty service payload: missing op byte".to_string())?;
        match op {
            OP_ECHO => Ok(ServiceRequest::Echo(rest.to_vec())),
            OP_UPPER => Ok(ServiceRequest::Upper(rest.to_vec())),
            OP_SLOW => {
                if rest.len() < 4 {
                    return Err("SLOW requires 4-byte delay_ms".into());
                }
                let delay_ms = u32::from_be_bytes([rest[0], rest[1], rest[2], rest[3]]);
                Ok(ServiceRequest::Slow {
                    delay_ms,
                    body: rest[4..].to_vec(),
                })
            }
            OP_ADD => {
                if rest.len() != 16 {
                    return Err(format!("ADD requires 16 args bytes, got {}", rest.len()));
                }
                let a = u64::from_be_bytes(rest[0..8].try_into().unwrap());
                let b = u64::from_be_bytes(rest[8..16].try_into().unwrap());
                Ok(ServiceRequest::Add(a, b))
            }
            OP_FAIL => Ok(ServiceRequest::Fail),
            other => Err(format!("unknown service op {other}")),
        }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match self {
            ServiceRequest::Echo(b) => {
                out.push(OP_ECHO);
                out.extend_from_slice(b);
            }
            ServiceRequest::Upper(b) => {
                out.push(OP_UPPER);
                out.extend_from_slice(b);
            }
            ServiceRequest::Slow { delay_ms, body } => {
                out.push(OP_SLOW);
                out.extend_from_slice(&delay_ms.to_be_bytes());
                out.extend_from_slice(body);
            }
            ServiceRequest::Add(a, b) => {
                out.push(OP_ADD);
                out.extend_from_slice(&a.to_be_bytes());
                out.extend_from_slice(&b.to_be_bytes());
            }
            ServiceRequest::Fail => out.push(OP_FAIL),
        }
        out
    }
}

impl ServiceResponse {
    pub fn ok(body: impl Into<Vec<u8>>) -> Self {
        ServiceResponse::Ok(body.into())
    }

    pub fn app_error(msg: impl Into<String>) -> Self {
        ServiceResponse::AppError(msg.into())
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match self {
            ServiceResponse::Ok(body) => {
                out.push(STATUS_OK);
                out.extend_from_slice(body);
            }
            ServiceResponse::AppError(msg) => {
                out.push(STATUS_APP_ERROR);
                out.extend_from_slice(msg.as_bytes());
            }
        }
        out
    }

    /// Decode a service response from a frame payload. `frame_error` is the
    /// transport-level error carried by an ERROR frame, if that is what
    /// arrived (passed through unchanged here for callers that already
    /// distinguish it).
    pub fn decode(payload: &[u8]) -> Result<ServiceResponse, RpcAppError> {
        let (&status, rest) = payload
            .split_first()
            .ok_or_else(|| RpcAppError("empty service response".into()))?;
        match status {
            STATUS_OK => Ok(ServiceResponse::Ok(rest.to_vec())),
            STATUS_APP_ERROR => Ok(ServiceResponse::AppError(
                String::from_utf8_lossy(rest).into_owned(),
            )),
            other => Err(RpcAppError(format!("unknown service status {other}"))),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpcAppError(pub String);

/// Application error code used inside transport ERROR frames when the service
/// payload itself cannot be decoded.
pub const APP_DECODE_ERROR: ErrorCode = ErrorCode::AppError;
