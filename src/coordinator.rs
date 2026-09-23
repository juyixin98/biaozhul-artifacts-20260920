//! 协调服务的 HTTP 接口（Axum）：
//!
//! | 方法 | 路径 | 说明 |
//! |---|---|---|
//! | POST | /v1/messages | 投递消息（分配通道内连续 nonce） |
//! | GET  | /v1/messages/{id} | 查询消息当前状态 |
//! | POST | /v1/leases/acquire?ttl_secs=… | 中继领取队头租约（204 = 暂无） |
//! | POST | /v1/leases/{id}/receipt | 中继回执（带 fencing token，旧代被拒） |
//! | POST | /v1/messages/{id}/retry | failed 消息重试，解除通道阻塞 |
//! | GET  | /v1/channels | 通道列表（看 blocked / next_nonce） |

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::Duration;
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use uuid::Uuid;

use crate::db::{ReceiptVerdict, Store, StoreError};

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

pub fn router(store: Arc<Store>) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/v1/messages", post(enqueue))
        .route("/v1/messages/{id}", get(get_message))
        .route("/v1/messages/{id}/retry", post(retry_message))
        .route("/v1/leases/acquire", post(acquire))
        .route("/v1/leases/{id}/receipt", post(receipt))
        .route("/v1/channels", get(list_channels))
        .route("/healthz", get(|| async { "ok" }))
        .with_state(state)
}

impl IntoResponse for StoreError {
    fn into_response(self) -> Response {
        let (status, code) = match &self {
            StoreError::ChannelBlocked(_) => (StatusCode::CONFLICT, "channel_blocked"),
            StoreError::NotFailed => (StatusCode::CONFLICT, "not_failed"),
            StoreError::NotFound => (StatusCode::NOT_FOUND, "not_found"),
            StoreError::Sqlx(_) => (StatusCode::INTERNAL_SERVER_ERROR, "db_error"),
        };
        (status, Json(json!({"error": code, "message": self.to_string()}))).into_response()
    }
}

#[derive(Deserialize)]
struct EnqueueBody {
    channel_id: String,
    payload: Value,
}

async fn enqueue(
    State(s): State<AppState>,
    Json(body): Json<EnqueueBody>,
) -> Result<Json<Value>, StoreError> {
    let m = s.store.enqueue(&body.channel_id, body.payload).await?;
    Ok(Json(message_json(&m)))
}

async fn get_message(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>, StoreError> {
    Ok(Json(message_json(&s.store.get(id).await?)))
}

async fn retry_message(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>, StoreError> {
    Ok(Json(message_json(&s.store.retry(id).await?)))
}

#[derive(Deserialize)]
struct AcquireQuery {
    relay_id: String,
    #[serde(default = "default_ttl")]
    ttl_secs: i64,
}

fn default_ttl() -> i64 {
    10
}

async fn acquire(
    State(s): State<AppState>,
    Query(q): Query<AcquireQuery>,
) -> Result<Response, StoreError> {
    let ttl = Duration::seconds(q.ttl_secs.clamp(1, 3600));
    match s.store.acquire(&q.relay_id, ttl).await? {
        Some(lease) => Ok((StatusCode::OK, Json(lease_json(&lease))).into_response()),
        None => Ok(StatusCode::NO_CONTENT.into_response()),
    }
}

#[derive(Deserialize)]
struct ReceiptBody {
    fencing_token: i64,
    #[serde(default)]
    tx_id: Option<String>,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<String>,
}

async fn receipt(
    State(s): State<AppState>,
    Path(id): Path<Uuid>,
    Json(body): Json<ReceiptBody>,
) -> Result<Json<Value>, StoreError> {
    let verdict = s
        .store
        .submit_receipt(
            id,
            body.fencing_token,
            body.tx_id.as_deref(),
            body.result,
            body.error.as_deref(),
        )
        .await?;

    let (accepted, code) = match verdict {
        ReceiptVerdict::Applied => (true, "applied"),
        ReceiptVerdict::StaleToken => (false, "stale_token"),
        ReceiptVerdict::AlreadyFinal => (false, "already_final"),
    };
    Ok(Json(json!({
        "accepted": accepted,
        "verdict": code,
        "message": message_json(&s.store.get(id).await?),
    })))
}

async fn list_channels(State(s): State<AppState>) -> Result<Json<Vec<Value>>, StoreError> {
    let rows: Vec<(String, i64, bool, Option<String>)> = sqlx::query_as(
        "SELECT channel_id, next_nonce, blocked, blocked_reason FROM channels ORDER BY channel_id",
    )
    .fetch_all(&s.store.pool)
    .await?;
    Ok(Json(
        rows.into_iter()
            .map(|(channel_id, next_nonce, blocked, blocked_reason)| {
                json!({
                    "channel_id": channel_id,
                    "next_nonce": next_nonce,
                    "blocked": blocked,
                    "blocked_reason": blocked_reason,
                })
            })
            .collect(),
    ))
}

fn message_json(m: &crate::db::Message) -> Value {
    json!({
        "submission_id": m.submission_id,
        "channel_id": m.channel_id,
        "nonce": m.nonce,
        "status": m.status,
        "fencing_token": m.fencing_token,
        "lease_owner": m.lease_owner,
        "lease_expires_at": m.lease_expires_at,
        "attempts": m.attempts,
        "target_tx_id": m.target_tx_id,
        "result": m.result,
        "error": m.error,
        "payload": m.payload,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    })
}

fn lease_json(l: &crate::db::Lease) -> Value {
    json!({
        "submission_id": l.submission_id,
        "channel_id": l.channel_id,
        "nonce": l.nonce,
        "payload": l.payload,
        "fencing_token": l.fencing_token,
        "lease_owner": l.lease_owner,
        "lease_expires_at": l.lease_expires_at,
        "attempts": l.attempts,
    })
}
