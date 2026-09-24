//! API error type mapped to JSON error responses.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug)]
pub struct Error {
    pub status: StatusCode,
    pub message: String,
}

impl Error {
    pub fn bad_request<M: Into<String>>(m: M) -> Self {
        Error {
            status: StatusCode::BAD_REQUEST,
            message: m.into(),
        }
    }

    pub fn not_found<M: Into<String>>(m: M) -> Self {
        Error {
            status: StatusCode::NOT_FOUND,
            message: m.into(),
        }
    }

    pub fn conflict<M: Into<String>>(m: M) -> Self {
        Error {
            status: StatusCode::CONFLICT,
            message: m.into(),
        }
    }

    pub fn payload_too_large<M: Into<String>>(m: M) -> Self {
        Error {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            message: m.into(),
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.status, self.message)
    }
}

impl std::error::Error for Error {}

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let body = Json(json!({ "error": self.message }));
        (self.status, body).into_response()
    }
}

/// Convenient result alias.
pub type Result<T> = std::result::Result<T, Error>;

/// Convert multipart failures into 400s.
impl From<axum::extract::multipart::MultipartError> for Error {
    fn from(e: axum::extract::multipart::MultipartError) -> Self {
        Error::bad_request(format!("multipart error: {e}"))
    }
}
