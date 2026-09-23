//! HTTP 接口层（Axum）：把事务/快照/GC 操作暴露为 JSON HTTP API。
//!
//! 字节型键值采用 URL-safe base64（字母表 - 与 _，无裸 '/' '+'）传输，
//! 可直接放入 URL 路径段；填充用的 '=' 也是路径合法字符。

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::mvcc::{CommitError, MvccStore};

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<MvccStore>,
}

pub fn router(db: Arc<MvccStore>) -> Router {
    Router::new()
        .route("/health", get(health))
        // 事务
        .route("/txn", post(begin))
        .route("/txn/{txn_id}/commit", post(commit))
        .route("/txn/{txn_id}/rollback", post(rollback))
        .route("/txn/{txn_id}/get/{key}", get(get_txn))
        .route("/txn/{txn_id}/put/{key}", post(put_txn))
        .route("/txn/{txn_id}/delete/{key}", post(delete_txn))
        // 只读快照
        .route("/snapshot", post(create_snapshot))
        .route("/snapshot/{snap_id}", post(close_snapshot))
        .route("/snapshot/{snap_id}/get/{key}", get(get_snapshot))
        // 管理/诊断
        .route("/admin/gc", post(run_gc))
        .route("/admin/watermark", get(watermark))
        .route("/admin/scan/{ts}", get(scan_at))
        .route("/admin/versions/{key}", get(debug_versions))
        .with_state(AppState { db })
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

// ---------- 请求/响应体 ----------

#[derive(Deserialize)]
struct ValueBody {
    /// base64 编码的值。
    value: String,
}

#[derive(Serialize)]
struct BeginResp {
    txn_id: u64,
    start_ts: u64,
}

#[derive(Serialize)]
struct CommitResp {
    committed: bool,
    commit_ts: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    conflict_key: Option<String>,
}

#[derive(Serialize)]
struct ValueResp {
    /// base64 编码值；字段为 null 表示键不存在或已被删除。
    value: Option<String>,
}

#[derive(Serialize)]
struct SnapshotResp {
    snapshot_id: u64,
    snap_ts: u64,
}

fn b64e(b: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE.encode(b)
}

fn b64d(s: &str) -> Result<Vec<u8>, (StatusCode, String)> {
    use base64::Engine;
    // 兼容无填充输入。
    base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(s.trim_end_matches('=').as_bytes())
        .map_err(|e| {
            (
                StatusCode::BAD_REQUEST,
                format!("invalid base64: {e}"),
            )
        })
}

fn key_b64(k: &str) -> Result<Vec<u8>, (StatusCode, String)> {
    b64d(k)
}

// ---------- handler ----------

async fn begin(State(st): State<AppState>) -> Json<BeginResp> {
    let (txn_id, start_ts) = st.db.begin();
    Json(BeginResp { txn_id, start_ts })
}

async fn commit(
    State(st): State<AppState>,
    Path(txn_id): Path<u64>,
) -> Response {
    match st.db.commit(txn_id) {
        Ok(commit_ts) => Json(CommitResp {
            committed: true,
            commit_ts: Some(commit_ts),
            error: None,
            conflict_key: None,
        })
        .into_response(),
        Err(CommitError::WriteWriteConflict { key }) => (
            StatusCode::CONFLICT,
            Json(CommitResp {
                committed: false,
                commit_ts: None,
                error: Some("write-write conflict".into()),
                conflict_key: Some(b64e(&key)),
            }),
        )
            .into_response(),
        Err(CommitError::NotFound) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "transaction not found" })),
        )
            .into_response(),
    }
}

async fn rollback(
    State(st): State<AppState>,
    Path(txn_id): Path<u64>,
) -> Response {
    if st.db.rollback(txn_id) {
        Json(serde_json::json!({ "rolled_back": txn_id })).into_response()
    } else {
        (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "transaction not found" })),
        )
            .into_response()
    }
}

async fn get_txn(
    State(st): State<AppState>,
    Path((txn_id, key)): Path<(u64, String)>,
) -> Result<Json<ValueResp>, (StatusCode, String)> {
    let key = key_b64(&key)?;
    match st.db.get(txn_id, &key) {
        Ok(v) => Ok(Json(ValueResp {
            value: v.as_deref().map(b64e),
        })),
        Err(()) => Err((
            StatusCode::NOT_FOUND,
            "transaction not found".to_string(),
        )),
    }
}

async fn put_txn(
    State(st): State<AppState>,
    Path((txn_id, key)): Path<(u64, String)>,
    Json(body): Json<ValueBody>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let key = key_b64(&key)?;
    let value = b64d(&body.value)?;
    st.db.put(txn_id, key, value).map_err(|_| {
        (
            StatusCode::NOT_FOUND,
            "transaction not found".to_string(),
        )
    })?;
    Ok(Json(serde_json::json!({ "status": "buffered" })))
}

async fn delete_txn(
    State(st): State<AppState>,
    Path((txn_id, key)): Path<(u64, String)>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let key = key_b64(&key)?;
    st.db.delete(txn_id, key).map_err(|_| {
        (
            StatusCode::NOT_FOUND,
            "transaction not found".to_string(),
        )
    })?;
    Ok(Json(serde_json::json!({ "status": "tombstone_buffered" })))
}

async fn create_snapshot(State(st): State<AppState>) -> Json<SnapshotResp> {
    let (snapshot_id, snap_ts) = st.db.create_snapshot();
    Json(SnapshotResp {
        snapshot_id,
        snap_ts,
    })
}

async fn close_snapshot(
    State(st): State<AppState>,
    Path(snap_id): Path<u64>,
) -> Response {
    if st.db.close_snapshot(snap_id) {
        Json(serde_json::json!({ "closed": snap_id })).into_response()
    } else {
        (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "snapshot not found" })),
        )
            .into_response()
    }
}

async fn get_snapshot(
    State(st): State<AppState>,
    Path((snap_id, key)): Path<(u64, String)>,
) -> Result<Json<ValueResp>, (StatusCode, String)> {
    let key = key_b64(&key)?;
    let v = st
        .db
        .snapshot_read(snap_id, &key)
        .map_err(|_| (StatusCode::NOT_FOUND, "snapshot not found".to_string()))?;
    Ok(Json(ValueResp {
        value: v.as_deref().map(|b| b64e(b)),
    }))
}

async fn run_gc(State(st): State<AppState>) -> Json<crate::mvcc::GcStats> {
    Json(st.db.gc())
}

async fn watermark(State(st): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "watermark": st.db.watermark() }))
}

async fn scan_at(
    State(st): State<AppState>,
    Path(ts): Path<u64>,
) -> Json<serde_json::Value> {
    let pairs = st.db.scan_at(ts);
    let items: Vec<serde_json::Value> = pairs
        .into_iter()
        .map(|(k, v)| serde_json::json!({ "key": b64e(&k), "value": b64e(&v) }))
        .collect();
    Json(serde_json::json!({ "snap_ts": ts, "items": items }))
}

async fn debug_versions(
    State(st): State<AppState>,
    Path(key): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let key = key_b64(&key)?;
    let vs = st.db.debug_versions(&key);
    let items: Vec<serde_json::Value> = vs
        .into_iter()
        .map(|(ts, tomb)| serde_json::json!({ "commit_ts": ts, "tombstone": tomb }))
        .collect();
    Ok(Json(serde_json::json!({ "versions": items })))
}
