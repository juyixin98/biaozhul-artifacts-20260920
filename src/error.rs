use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// Errors returned by the storage layer, mapped to HTTP responses.
#[derive(Debug)]
pub enum AppError {
    /// 404 — upload session or object does not exist.
    NotFound(String),
    /// 409 — object already committed (immutability conflict).
    Conflict(String),
    /// 409 — a part with this number was uploaded before with different content.
    ConflictPart(String),
    /// 422 — completing an upload whose parts are missing/inconsistent.
    Unprocessable(String),
    /// 400 — malformed request.
    BadRequest(String),
    /// 500 — unexpected IO / internal error.
    Internal(String),
}

impl AppError {
    fn parts(&self) -> (StatusCode, &'static str, &str) {
        match self {
            AppError::NotFound(m) => (StatusCode::NOT_FOUND, "not_found", m),
            AppError::Conflict(m) => (StatusCode::CONFLICT, "conflict", m),
            AppError::ConflictPart(m) => (StatusCode::CONFLICT, "part_conflict", m),
            AppError::Unprocessable(m) => {
                (StatusCode::UNPROCESSABLE_ENTITY, "unprocessable_entity", m)
            }
            AppError::BadRequest(m) => (StatusCode::BAD_REQUEST, "bad_request", m),
            AppError::Internal(m) => (StatusCode::INTERNAL_SERVER_ERROR, "internal", m),
        }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let (_, code, msg) = self.parts();
        write!(f, "{code}: {msg}")
    }
}

impl std::error::Error for AppError {}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, code, message) = self.parts();
        (status, Json(json!({ "error": code, "message": message }))).into_response()
    }
}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        match e.kind() {
            std::io::ErrorKind::NotFound => AppError::NotFound(e.to_string()),
            std::io::ErrorKind::AlreadyExists => AppError::Conflict(e.to_string()),
            _ => AppError::Internal(e.to_string()),
        }
    }
}

pub type AppResult<T> = Result<T, AppError>;
