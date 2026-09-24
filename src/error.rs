use serde::ser::{SerializeMap, Serializer};
use serde::Serialize;
use std::fmt;

#[derive(Debug, Clone, Copy)]
pub enum Reject {
    /// 401 未知来源
    UnknownSource,
    /// 401 缺少/非法签名头
    BadSignatureHeader,
    /// 401 HMAC 校验失败
    BadSignature,
    /// 400 请求体不是合法 JSON
    BadJson,
    /// 422 字段缺失或非法
    BadPayload,
    /// 422 序号不新（<= 已接受的最大序号）
    StaleSequence,
    /// 422 nonce 重复（重放）
    ReplayedNonce,
    /// 422 命令已过有效期
    Expired,
    /// 422 issue_ms 过于超前
    TooFarInFuture,
    /// 422 租约超出服务器上限
    LeaseTooLong,
    /// 422 清除急停时当前并未锁存
    EstopNotActive,
    /// 409 数据库唯一约束竞争（极少数并发情况）
    Conflict,
    /// 404 尚无决策记录
    NoDecision,
    /// 500 内部错误（数据库等）
    Internal,
}

impl Reject {
    pub fn status(self) -> u16 {
        match self {
            Reject::UnknownSource | Reject::BadSignatureHeader | Reject::BadSignature => 401,
            Reject::BadJson => 400,
            Reject::BadPayload
            | Reject::StaleSequence
            | Reject::ReplayedNonce
            | Reject::Expired
            | Reject::TooFarInFuture
            | Reject::LeaseTooLong
            | Reject::EstopNotActive => 422,
            Reject::Conflict => 409,
            Reject::NoDecision => 404,
            Reject::Internal => 500,
        }
    }

    pub fn reason(self) -> &'static str {
        match self {
            Reject::UnknownSource => "unknown_source",
            Reject::BadSignatureHeader => "bad_signature_header",
            Reject::BadSignature => "bad_signature",
            Reject::BadJson => "bad_json",
            Reject::BadPayload => "bad_payload",
            Reject::StaleSequence => "stale_sequence",
            Reject::ReplayedNonce => "replayed_nonce",
            Reject::Expired => "expired",
            Reject::TooFarInFuture => "too_far_in_future",
            Reject::LeaseTooLong => "lease_too_long",
            Reject::EstopNotActive => "estop_not_active",
            Reject::Conflict => "conflict",
            Reject::NoDecision => "no_decision",
            Reject::Internal => "internal_error",
        }
    }
}

impl fmt::Display for Reject {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.reason())
    }
}

impl std::error::Error for Reject {}

/// 内部错误：包含机器可读 reason（写入 ingest_log）与 HTTP 状态。
#[derive(Debug)]
pub struct AppError {
    pub reject: Reject,
    pub detail: String,
}

impl AppError {
    pub fn new(reject: Reject, detail: impl Into<String>) -> Self {
        AppError {
            reject,
            detail: detail.into(),
        }
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.reject.reason(), self.detail)
    }
}

impl std::error::Error for AppError {}

impl From<rusqlite::Error> for AppError {
    fn from(e: rusqlite::Error) -> Self {
        // 唯一约束冲突映射为 409，其余视为 500。
        if let rusqlite::Error::SqliteFailure(ref err, _) = e {
            if err.code == rusqlite::ErrorCode::ConstraintViolation {
                return AppError::new(Reject::Conflict, e.to_string());
            }
        }
        AppError::new(Reject::Internal, format!("database error: {e}"))
    }
}

/// 统一 JSON 错误体 `{"error":"<reason>","detail":"..."}`。
impl Serialize for AppError {
    fn serialize<S>(&self, ser: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut m = ser.serialize_map(Some(2))?;
        m.serialize_entry("error", self.reject.reason())?;
        m.serialize_entry("detail", &self.detail)?;
        m.end()
    }
}

impl axum::response::IntoResponse for AppError {
    fn into_response(self) -> axum::response::Response {
        let status = axum::http::StatusCode::from_u16(self.reject.status())
            .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        if status.is_server_error() {
            tracing::error!(error = %self, "request failed");
        }
        (status, axum::Json(self)).into_response()
    }
}
