//! Error type shared by the wire codec, engine and HTTP layer.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// All fallible operations in this crate return this error.
#[derive(Debug)]
pub enum Error {
    /// Artifact name not present in the in-memory store.
    NotFound(String),
    /// Malformed signature or patch byte stream.
    Malformed(&'static str),
    /// Supplied block size out of the allowed range.
    BadBlockSize(u32),
    /// Invalid artifact name in the URL.
    BadName,
    /// A REF/LITERAL operation went out of bounds while applying.
    OutOfBounds(&'static str),
    /// Reconstructed output did not match the expected length.
    LengthMismatch { expected: u64, actual: u64 },
    /// Reconstructed output did not match the strong digest carried in the patch.
    HashMismatch,
    /// The supplied basis did not match the digest the patch was built against.
    BasisMismatch,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::NotFound(n) => write!(f, "artifact not found: {n}"),
            Error::Malformed(why) => write!(f, "malformed message: {why}"),
            Error::BadBlockSize(s) => write!(f, "block size out of range (1..=65536): {s}"),
            Error::BadName => write!(f, "invalid artifact name"),
            Error::OutOfBounds(why) => write!(f, "operation out of bounds: {why}"),
            Error::LengthMismatch { expected, actual } => {
                write!(f, "target length mismatch: header says {expected}, got {actual}")
            }
            Error::HashMismatch => write!(f, "strong hash mismatch: reconstructed bytes differ"),
            Error::BasisMismatch => write!(f, "basis hash mismatch: wrong old content for this patch"),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let code = match &self {
            Error::NotFound(_) => StatusCode::NOT_FOUND,
            Error::Malformed(_) | Error::BadBlockSize(_) | Error::BadName => StatusCode::BAD_REQUEST,
            Error::OutOfBounds(_) | Error::LengthMismatch { .. } => StatusCode::UNPROCESSABLE_ENTITY,
            Error::HashMismatch | Error::BasisMismatch => StatusCode::CONFLICT,
        };
        (
            code,
            Json(json!({ "error": self.to_string() })),
        )
            .into_response()
    }
}
