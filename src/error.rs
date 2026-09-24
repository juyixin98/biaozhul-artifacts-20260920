//! Error types surfaced over HTTP.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// All domain errors. Each variant maps to a stable error `code` and HTTP
/// status so clients can distinguish "not found" from "ambiguous".
#[derive(Debug, Clone)]
pub enum ApiError {
    /// Malformed request body or query parameters.
    BadRequest(String),
    /// Repository, tag, or digest not present.
    NotFound(String),
    /// Stored content failed digest verification.
    DigestMismatch {
        claimed: String,
        actual: String,
        at: String,
    },
    /// A descriptor referenced a blob that is not stored locally.
    MissingBlob { digest: String, at: String },
    /// A child descriptor could not be parsed/classified.
    InvalidContent { message: String, at: String },
    /// The referenced cycle of indices/manifests was detected.
    Cycle { path: Vec<String> },
    /// No platform entry satisfied the requested constraints.
    NoMatch {
        message: String,
        tried: Vec<serde_json::Value>,
    },
    /// Several distinct manifests matched equally; refusing to pick one.
    Ambiguous {
        message: String,
        candidates: Vec<serde_json::Value>,
    },
}

pub type ApiResult<T> = Result<T, ApiError>;

impl ApiError {
    fn status_and_code(&self) -> (StatusCode, &'static str) {
        match self {
            ApiError::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
            ApiError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            ApiError::DigestMismatch { .. } => {
                (StatusCode::UNPROCESSABLE_ENTITY, "digest_mismatch")
            }
            ApiError::MissingBlob { .. } => (StatusCode::UNPROCESSABLE_ENTITY, "missing_blob"),
            ApiError::InvalidContent { .. } => {
                (StatusCode::UNPROCESSABLE_ENTITY, "invalid_content")
            }
            ApiError::Cycle { .. } => (StatusCode::CONFLICT, "manifest_cycle"),
            ApiError::NoMatch { .. } => (StatusCode::NOT_FOUND, "no_match"),
            ApiError::Ambiguous { .. } => (StatusCode::CONFLICT, "ambiguous"),
        }
    }

    fn detail(&self) -> serde_json::Value {
        match self {
            ApiError::BadRequest(m) | ApiError::NotFound(m) => json!({ "message": m }),
            ApiError::DigestMismatch {
                claimed,
                actual,
                at,
            } => json!({
                "message": "content digest verification failed",
                "claimed": claimed,
                "actual": actual,
                "at": at,
            }),
            ApiError::MissingBlob { digest, at } => json!({
                "message": "referenced blob is not available locally; the service never downloads content",
                "digest": digest,
                "at": at,
            }),
            ApiError::InvalidContent { message, at } => json!({
                "message": message,
                "at": at,
            }),
            ApiError::Cycle { path } => json!({
                "message": "manifest reference cycle detected",
                "path": path,
            }),
            ApiError::NoMatch { message, tried } => json!({
                "message": message,
                "examined": tried,
            }),
            ApiError::Ambiguous {
                message,
                candidates,
            } => json!({
                "message": message,
                "candidates": candidates,
            }),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code) = self.status_and_code();
        let mut body = self.detail();
        body.as_object_mut()
            .unwrap()
            .insert("code".to_string(), json!(code));
        (status, Json(body)).into_response()
    }
}
