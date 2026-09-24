//! Error type shared between extraction logic and HTTP layer.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    // --- Image / layer content problems -> 422 ---
    #[error("oci layout error: {0}")]
    Layout(String),
    #[error("manifest error: {0}")]
    Manifest(String),
    #[error("digest mismatch for {descriptor}: expected {expected}, got {actual}")]
    DigestMismatch {
        descriptor: String,
        expected: String,
        actual: String,
    },
    #[error("corrupt or truncated archive ({0})")]
    CorruptArchive(String),
    #[error("rejected entry `{path}`: {reason}")]
    RejectedEntry { path: String, reason: String },
    #[error("extraction limit exceeded: {0}")]
    Limit(String),
    #[error("whiteout error: {0}")]
    Whiteout(String),

    // --- Request problems -> 400/404 ---
    #[error("invalid image name: {0}")]
    BadImageName(String),
    #[error("image not found: {0}")]
    NotFound(String),

    // --- Server / environment problems -> 500 ---
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl AppError {
    pub fn layout(msg: impl Into<String>) -> Self {
        Self::Layout(msg.into())
    }
    pub fn manifest(msg: impl Into<String>) -> Self {
        Self::Manifest(msg.into())
    }
    pub fn rejected(path: impl Into<String>, reason: impl Into<String>) -> Self {
        Self::RejectedEntry {
            path: path.into(),
            reason: reason.into(),
        }
    }
    pub fn limit(msg: impl Into<String>) -> Self {
        Self::Limit(msg.into())
    }
    pub fn corrupt(msg: impl Into<String>) -> Self {
        Self::CorruptArchive(msg.into())
    }

    fn classification(&self) -> (StatusCode, &'static str) {
        match self {
            AppError::Layout(_)
            | AppError::Manifest(_)
            | AppError::DigestMismatch { .. }
            | AppError::CorruptArchive(_)
            | AppError::RejectedEntry { .. }
            | AppError::Limit(_)
            | AppError::Whiteout(_) => (StatusCode::UNPROCESSABLE_ENTITY, "unprocessable"),
            AppError::BadImageName(_) => (StatusCode::BAD_REQUEST, "bad_request"),
            AppError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            AppError::Io(_) | AppError::Json(_) | AppError::Other(_) => {
                (StatusCode::INTERNAL_SERVER_ERROR, "internal")
            }
        }
    }
}

pub type AppResult<T> = Result<T, AppError>;

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, kind) = self.classification();
        // Internal errors may carry host path details; only log those, and
        // return a generic message to the client.
        if status == StatusCode::INTERNAL_SERVER_ERROR {
            tracing::error!(error = %self, "rebuild failed internally");
            return (
                status,
                Json(json!({ "error": kind, "message": "internal server error" })),
            )
                .into_response();
        }
        tracing::warn!(error = %self, "rebuild rejected");
        (
            status,
            Json(json!({ "error": kind, "message": self.to_string() })),
        )
            .into_response()
    }
}
