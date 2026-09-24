//! 统一错误类型：服务层错误 -> HTTP 响应。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// 所有业务/基础设施错误。
#[derive(Debug, thiserror::Error)]
pub enum AppError {
    /// 404：制品或摘要对应内容不存在。
    #[error("not found: {0}")]
    NotFound(String),

    /// 400：请求体或字段不合法。
    #[error("bad request: {0}")]
    BadRequest(String),

    /// 409：状态机冲突（非法跃迁、重复晋级、内容已被绑定不可替换、并发冲突等）。
    #[error("conflict: {0}")]
    Conflict(String),

    /// 422：门禁未通过（缺审批或缺必要证明）。
    #[error("precondition failed: {0}")]
    Precondition(String),
}

impl AppError {
    pub fn not_found(msg: impl Into<String>) -> Self {
        Self::NotFound(msg.into())
    }
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self::BadRequest(msg.into())
    }
    pub fn conflict(msg: impl Into<String>) -> Self {
        Self::Conflict(msg.into())
    }
    pub fn precondition(msg: impl Into<String>) -> Self {
        Self::Precondition(msg.into())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, code, message) = match self {
            AppError::NotFound(m) => (StatusCode::NOT_FOUND, "not_found", m),
            AppError::BadRequest(m) => (StatusCode::BAD_REQUEST, "bad_request", m),
            AppError::Conflict(m) => (StatusCode::CONFLICT, "conflict", m),
            AppError::Precondition(m) => {
                (StatusCode::UNPROCESSABLE_ENTITY, "precondition_failed", m)
            }
        };
        (status, Json(json!({ "error": code, "message": message }))).into_response()
    }
}

pub type AppResult<T> = Result<T, AppError>;
