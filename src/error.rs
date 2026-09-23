use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug)]
pub enum AppError {
    /// 400 — malformed request (bad hex, empty batch, bad version, ...).
    BadRequest(String),
    /// 404 — unknown version / no root published yet.
    NotFound(String),
    /// 500 — storage failure. Commits are atomic: on this path no root is
    /// ever published (the write batch is discarded).
    Storage(String),
}

impl AppError {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        AppError::BadRequest(msg.into())
    }

    pub fn not_found(msg: impl Into<String>) -> Self {
        AppError::NotFound(msg.into())
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AppError::BadRequest(m) => write!(f, "bad request: {m}"),
            AppError::NotFound(m) => write!(f, "not found: {m}"),
            AppError::Storage(m) => write!(f, "storage error: {m}"),
        }
    }
}

impl std::error::Error for AppError {}

impl From<rocksdb::Error> for AppError {
    fn from(e: rocksdb::Error) -> Self {
        AppError::Storage(e.to_string())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, kind, message) = match self {
            AppError::BadRequest(m) => (StatusCode::BAD_REQUEST, "bad_request", m),
            AppError::NotFound(m) => (StatusCode::NOT_FOUND, "not_found", m),
            AppError::Storage(m) => (StatusCode::INTERNAL_SERVER_ERROR, "storage_error", m),
        };
        (status, Json(json!({ "error": kind, "message": message }))).into_response()
    }
}

pub type AppResult<T> = Result<T, AppError>;
