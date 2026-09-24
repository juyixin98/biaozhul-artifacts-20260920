//! HTTP 接口层（Axum）。
//!
//! 路由：
//!
//! | 方法 | 路径 | 说明 |
//! |---|---|---|
//! | `GET`  | `/healthz` | 健康检查 |
//! | `PUT`  | `/blobs/{hash}?size_bytes=N` | 上传/确认 CAS 块（原始字节为 body） |
//! | `GET`  | `/blobs/{hash}` | 下载块（返回前服务端已全量校验）；可带头 `X-Expected-Size-Bytes` |
//! | `HEAD` | `/blobs/{hash}` | 探测块是否存在且完整 |
//! | `POST` | `/find-missing` | 批量返回缺失/损坏的摘要 |
//! | `PUT`  | `/actions/{hash}` | 发布动作结果（全部对象核验通过才落盘） |
//! | `GET`  | `/actions/{hash}` | 查询动作结果（命中前再次核验全部对象） |
//!
//! 另有 `POST /util/action-digest`（仅原型辅助）：返回动作的规范 JSON 与摘要，
//! 方便用 curl 手工构造请求；生产系统中摘要由客户端自行计算。

use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use serde::Deserialize;

use crate::models::{
    canonical_json, Digest, ErrorResponse, FindMissingRequest, PublishActionRequest,
};
use crate::store::{LocalStore, StoreError};

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<LocalStore>,
    /// 单次上传的最大字节数，防止请求体无限增长耗尽内存。
    pub max_blob_bytes: usize,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/blobs/:hash", put(put_blob).get(get_blob).head(head_blob))
        .route("/find-missing", post(find_missing))
        .route("/actions/:hash", put(put_action).get(get_action))
        .route("/util/action-digest", post(action_digest))
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok\n"
}

#[derive(Debug, Deserialize)]
struct SizeQuery {
    size_bytes: u64,
}

// ---------------- CAS handlers ----------------

async fn put_blob(
    State(st): State<AppState>,
    Path(hash): Path<String>,
    Query(q): Query<SizeQuery>,
    body: Body,
) -> Result<Response, ApiError> {
    let data = read_body_limited(body, st.max_blob_bytes).await?;
    if data.len() as u64 != q.size_bytes {
        return Err(ApiError::from(StoreError::HashMismatch {
            claimed: format!("declared size {}", q.size_bytes),
            actual: format!("actual size {}", data.len()),
        }));
    }
    let digest = Digest {
        hash,
        size_bytes: q.size_bytes,
    };
    st.store.put_blob(&digest, &data).await?;
    // 幂等上传：块已存在且完整也返回 200 + 摘要。
    Ok((
        StatusCode::OK,
        Json(serde_json::json!({ "status": "stored", "digest": digest })),
    )
        .into_response())
}

async fn get_blob(
    State(st): State<AppState>,
    Path(hash): Path<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    // 下载验证摘要：返回前依据磁盘实际内容做全量哈希校验，损坏即 503，绝不返回坏数据。
    // 调用方可带头 X-Expected-Size-Bytes 交叉校验长度；缺省时长度取自文件元数据。
    let data = match expected_size(&headers) {
        Some(size) => st
            .store
            .get_blob(&Digest {
                hash,
                size_bytes: size,
            })
            .await
            .map_err(blob_read_error)?,
        None => st
            .store
            .get_blob_by_hash(&hash)
            .await
            .map_err(blob_read_error)?,
    };
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/octet-stream")],
        data,
    )
        .into_response())
}

/// 下载路径上的错误映射：不存在 → 404；损坏 → 503。
fn blob_read_error(e: StoreError) -> ApiError {
    match e {
        StoreError::MissingBlobs(_) => ApiError {
            status: StatusCode::NOT_FOUND,
            code: "not_found".into(),
            message: "blob not found in CAS".into(),
        },
        other => ApiError::from(other),
    }
}

async fn head_blob(
    State(st): State<AppState>,
    Path(hash): Path<String>,
) -> Result<StatusCode, ApiError> {
    let exists = st.store.blob_exists_by_hash(&hash).await?;
    Ok(if exists {
        StatusCode::OK
    } else {
        StatusCode::NOT_FOUND
    })
}

async fn find_missing(
    State(st): State<AppState>,
    Json(req): Json<FindMissingRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let missing = st.store.find_missing(&req.digests).await?;
    Ok(Json(serde_json::json!({ "missing": missing })))
}

// ---------------- Action cache handlers ----------------

async fn put_action(
    State(st): State<AppState>,
    Path(hash): Path<String>,
    body: Body,
) -> Result<Response, ApiError> {
    let raw = read_body_limited(body, st.max_blob_bytes).await?;
    let req: PublishActionRequest = serde_json::from_slice(&raw)
        .map_err(|e| ApiError::bad(format!("invalid publish request json: {e}")))?;
    let saved = st
        .store
        .put_action_result(&hash, &req.action, req.action_result)
        .await?;
    Ok((
        StatusCode::OK,
        Json(serde_json::json!({ "status": "published", "action_result": saved })),
    )
        .into_response())
}

async fn get_action(
    State(st): State<AppState>,
    Path(hash): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    match st.store.get_action_result(&hash).await {
        Ok(result) => Ok(Json(
            serde_json::json!({ "status": "HIT", "action_result": result }),
        )),
        // 动作结果本身不存在 → 缓存未命中，客户端应重新执行动作。
        Err(StoreError::MissingBlobs(_)) => Err(ApiError {
            status: StatusCode::NOT_FOUND,
            code: "cache_miss".into(),
            message: "action result not cached; client must re-execute".into(),
        }),
        // 结果存在但引用对象损坏/缺失，或动作记录本身损坏 → 明确报错而非伪命中，
        // 客户端同样应重新执行并重新上传。
        Err(e @ StoreError::Corrupted { .. }) => Err(ApiError::from(e)),
        Err(e) => Err(ApiError::from(e)),
    }
}

async fn action_digest(
    Json(action): Json<crate::models::Action>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let bytes =
        canonical_json(&action).map_err(|e| ApiError::bad(format!("serialize failed: {e}")))?;
    let hash = crate::hash::digest_bytes(&bytes);
    Ok(Json(serde_json::json!({
        "hash": hash,
        "size_bytes": bytes.len(),
        "canonical_json": String::from_utf8_lossy(&bytes),
    })))
}

// ---------------- helpers ----------------

fn expected_size(headers: &HeaderMap) -> Option<u64> {
    headers
        .get("x-expected-size-bytes")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.parse().ok())
}

async fn read_body_limited(body: Body, max: usize) -> Result<axum::body::Bytes, ApiError> {
    axum::body::to_bytes(body, max).await.map_err(|e| ApiError {
        status: StatusCode::PAYLOAD_TOO_LARGE,
        code: "payload_too_large".into(),
        message: format!("request body rejected (limit {max} bytes): {e}"),
    })
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    code: String,
    message: String,
}

impl ApiError {
    fn bad(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request".into(),
            message: msg.into(),
        }
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        let (status, code) = match &e {
            StoreError::InvalidDigest(_) | StoreError::HashMismatch { .. } => {
                (StatusCode::BAD_REQUEST, "invalid_digest")
            }
            StoreError::ActionKeyMismatch { .. } => {
                (StatusCode::BAD_REQUEST, "action_key_mismatch")
            }
            // 发布前缺块：客户端可修复（先上传对象），424 Failed Dependency。
            StoreError::MissingBlobs(_) => (StatusCode::FAILED_DEPENDENCY, "missing_blobs"),
            // 对象损坏：绝不返回 2xx；503 表示“缓存当前不可信，请重新执行动作”。
            StoreError::Corrupted { .. } => {
                (StatusCode::SERVICE_UNAVAILABLE, "cache_corruption")
            }
            StoreError::ActionAlreadyExists(_) => (StatusCode::CONFLICT, "immutable_conflict"),
            StoreError::Io(_) => (StatusCode::INTERNAL_SERVER_ERROR, "internal"),
        };
        Self {
            status,
            code: code.into(),
            message: e.to_string(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorResponse {
            error: self.code,
            message: self.message,
        };
        (self.status, Json(body)).into_response()
    }
}

// 让编译器保留 LocalStore 的类型引用（文档用途）。
#[allow(dead_code)]
fn _assert_store(_s: &LocalStore) {}
