use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use std::io;

/// 服务端统一错误类型。业务错误码见 README「错误码」一节。
#[derive(Debug)]
pub enum Error {
    /// 400：请求体 JSON 非法 / 字段不合法（含 serde 反序列化失败）
    BadRequest(String),
    /// 404
    NotFound(String),
    /// 409：状态冲突（重复提交、重复创建等）
    Conflict(String),
    /// 422：资源已超时失效
    Expired(String),
    /// 429：预留将使字节数或对象数超过租户配额
    QuotaExceeded {
        detail: String,
        limit_bytes: u64,
        used_bytes: u64,   // 已提交实占字节
        held_bytes: u64,   // 活跃预留字节（不含本次）
        want_bytes: u64,   // 本次申请字节
        limit_objects: u64,
        used_objects: u64,
        held_objects: u64,
        want_objects: u64,
    },
    /// 500：事件日志持久化失败
    Storage(String),
    /// 400：仅注入时钟模式支持
    ClockNotControllable,
}

impl Error {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Error::BadRequest(msg.into())
    }

    pub fn not_found(msg: impl Into<String>) -> Self {
        Error::NotFound(msg.into())
    }

    pub fn conflict(msg: impl Into<String>) -> Self {
        Error::Conflict(msg.into())
    }

    pub fn expired(msg: impl Into<String>) -> Self {
        Error::Expired(msg.into())
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Storage(e.to_string())
    }
}

/// serde_json 反序列化错误（Json 提取器）统一归为 400。
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::BadRequest(format!("invalid JSON body: {e}"))
    }
}

pub type AppResult<T> = Result<T, Error>;

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let (status, code, detail) = match self {
            Error::BadRequest(m) => (StatusCode::BAD_REQUEST, "bad_request", m),
            Error::NotFound(m) => (StatusCode::NOT_FOUND, "not_found", m),
            Error::Conflict(m) => (StatusCode::CONFLICT, "conflict", m),
            Error::Expired(m) => (StatusCode::UNPROCESSABLE_ENTITY, "expired", m),
            Error::QuotaExceeded {
                detail,
                limit_bytes,
                used_bytes,
                held_bytes,
                want_bytes,
                limit_objects,
                used_objects,
                held_objects,
                want_objects,
            } => {
                let body = (
                    StatusCode::TOO_MANY_REQUESTS,
                    Json(json!({
                        "error": "quota_exceeded",
                        "detail": detail,
                        "limit_bytes": limit_bytes,
                        "used_bytes": used_bytes,
                        "held_bytes": held_bytes,
                        "want_bytes": want_bytes,
                        "limit_objects": limit_objects,
                        "used_objects": used_objects,
                        "held_objects": held_objects,
                        "want_objects": want_objects,
                    })),
                );
                return body.into_response();
            }
            Error::Storage(m) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "storage_error",
                format!("persistence failure: {m}"),
            ),
            Error::ClockNotControllable => (
                StatusCode::BAD_REQUEST,
                "clock_not_controllable",
                "server is running with the system clock; restart with --clock injected to control time".to_string(),
            ),
        };
        (
            status,
            Json(json!({ "error": code, "detail": detail })),
        )
            .into_response()
    }
}
