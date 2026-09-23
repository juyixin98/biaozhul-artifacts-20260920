//! 协调器 HTTP API（Axum）。
//! 客户端接口：POST /enqueue、GET /submissions/:id、GET /channels/:id/submissions、
//!             POST /submissions/:id/requeue
//! 中继内部接口：POST /internal/claim|heartbeat|delivered|complete|report
//! 所有内部写接口都强制 (fence, relay_id) 双重校验，旧代次回执得到 409。

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::PgPool;
use std::time::Duration;

#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub lease_secs: i32,
}

pub fn router(pool: PgPool, lease_secs: i32) -> Router {
    Router::new()
        .route("/healthz", get(|| async { Json(json!({"ok": true})) }))
        .route("/enqueue", post(enqueue))
        .route("/submissions/{id}", get(get_submission))
        .route("/channels/{id}/submissions", get(list_channel))
        .route("/submissions/{id}/requeue", post(requeue))
        .route("/internal/claim", post(claim))
        .route("/internal/heartbeat", post(heartbeat))
        .route("/internal/delivered", post(delivered))
        .route("/internal/complete", post(complete))
        .route("/internal/report", post(report))
        .with_state(AppState { pool, lease_secs })
}

#[derive(Debug)]
enum GateError {
    BadRequest(String),
    NotFound(String),
    /// fence/relay 不匹配，或对象已离开当前租约状态
    Stale(String),
    Internal(anyhow::Error),
}

impl IntoResponse for GateError {
    fn into_response(self) -> Response {
        let (status, msg) = match self {
            GateError::BadRequest(m) => (StatusCode::BAD_REQUEST, m),
            GateError::NotFound(m) => (StatusCode::NOT_FOUND, m),
            GateError::Stale(m) => (StatusCode::CONFLICT, m),
            GateError::Internal(e) => {
                tracing::error!("internal error: {e:#}");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".to_string())
            }
        };
        (status, Json(json!({"error": msg}))).into_response()
    }
}

fn gate<E: std::fmt::Display>(e: E) -> GateError {
    GateError::Internal(anyhow::anyhow!("{e}"))
}

async fn stale_audit(pool: &PgPool, id: &str, req: &FenceReq, kind: &str) {
    let _ = sqlx::query(
        "INSERT INTO delivery_attempts (submission_id, relay_id, fence, event, detail)
         VALUES ($1,$2,$3,'stale',$4)",
    )
    .bind(id)
    .bind(&req.relay_id)
    .bind(req.fence)
    .bind(kind)
    .execute(pool)
    .await;
}

fn public(row: &crate::db::Submission) -> Value {
    json!({
        "id": row.id,
        "channel_id": row.channel_id,
        "seq": row.seq,
        "nonce": row.nonce,
        "payload": row.payload.0,
        "status": row.status,
        "fence": row.fence,
        "leased_by": row.leased_by,
        "attempts": row.attempts,
        "tx_hash": row.tx_hash,
        "last_error": row.last_error,
    })
}

// ---------- 客户端接口 ----------

#[derive(Deserialize)]
struct CommitIn {
    id: String,
    payload: Value,
}

#[derive(Deserialize)]
struct EnqueueReq {
    channel_id: String,
    commits: Vec<CommitIn>,
}

async fn enqueue(
    State(st): State<AppState>,
    Json(req): Json<EnqueueReq>,
) -> Result<Json<Value>, GateError> {
    if req.channel_id.trim().is_empty() {
        return Err(GateError::BadRequest("channel_id required".into()));
    }
    if req.commits.is_empty() {
        return Err(GateError::BadRequest("commits must not be empty".into()));
    }
    for c in &req.commits {
        if c.id.trim().is_empty() {
            return Err(GateError::BadRequest("commit id required".into()));
        }
    }
    let items: Vec<(String, Value)> =
        req.commits.into_iter().map(|c| (c.id, c.payload)).collect();
    let rows = crate::db::enqueue(&st.pool, &req.channel_id, &items)
        .await
        .map_err(gate)?;
    Ok(Json(json!({
        "enqueued": rows.iter().map(public).collect::<Vec<_>>().len(),
        "submissions": rows.iter().map(public).collect::<Vec<_>>(),
    })))
}

async fn get_submission(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, GateError> {
    let row = crate::db::get(&st.pool, &id).await.map_err(gate)?;
    match row {
        Some(r) => Ok(Json(public(&r))),
        None => Err(GateError::NotFound(format!("submission {id} not found"))),
    }
}

#[derive(Deserialize)]
struct ListQuery {
    limit: Option<i64>,
}

async fn list_channel(
    State(st): State<AppState>,
    Path(channel_id): Path<String>,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, GateError> {
    let limit = q.limit.unwrap_or(100).clamp(1, 1000);
    let rows = crate::db::list(&st.pool, &channel_id, limit)
        .await
        .map_err(gate)?;
    Ok(Json(json!({
        "channel_id": channel_id,
        "submissions": rows.iter().map(public).collect::<Vec<_>>(),
    })))
}

async fn requeue(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, GateError> {
    match crate::db::requeue(&st.pool, &id).await.map_err(gate)? {
        Some(fence) => Ok(Json(json!({"id": id, "status": "pending", "fence": fence}))),
        None => Err(GateError::NotFound(format!(
            "submission {id} not found or not in failed status"
        ))),
    }
}

// ---------- 中继内部接口 ----------

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ClaimResp {
    pub id: String,
    pub channel_id: String,
    pub nonce: i64,
    pub fence: i32,
    pub payload: Value,
    pub leased_until_unix: i64,
}

#[derive(Deserialize)]
struct ClaimReq {
    relay_id: String,
}

async fn claim(
    State(st): State<AppState>,
    Json(req): Json<ClaimReq>,
) -> Result<Response, GateError> {
    if req.relay_id.trim().is_empty() {
        return Err(GateError::BadRequest("relay_id required".into()));
    }
    let row = crate::db::claim(&st.pool, &req.relay_id, st.lease_secs)
        .await
        .map_err(gate)?;
    match row {
        Some(r) => {
            let leased_until = r.leased_until.ok_or_else(|| {
                GateError::Internal(anyhow::anyhow!("leased row missing leased_until"))
            })?;
            let unix = leased_until.timestamp();
            let body = ClaimResp {
                id: r.id,
                channel_id: r.channel_id,
                nonce: r.nonce.ok_or_else(|| {
                    GateError::Internal(anyhow::anyhow!("leased row missing nonce"))
                })?,
                fence: r.fence,
                payload: r.payload.0,
                leased_until_unix: unix,
            };
            Ok((StatusCode::OK, Json(body)).into_response())
        }
        None => Ok(StatusCode::NO_CONTENT.into_response()),
    }
}

#[derive(Deserialize)]
pub(crate) struct FenceReq {
    pub(crate) id: String,
    pub(crate) fence: i32,
    pub(crate) relay_id: String,
}

async fn heartbeat(
    State(st): State<AppState>,
    Json(req): Json<FenceReq>,
) -> Result<Json<Value>, GateError> {
    let ok = crate::db::heartbeat(&st.pool, &req.id, req.fence, &req.relay_id, st.lease_secs)
        .await
        .map_err(gate)?;
    if !ok {
        stale_audit(&st.pool, &req.id, &req, "heartbeat").await;
        return Err(GateError::Stale("lease lost or stale fence".into()));
    }
    Ok(Json(json!({"ok": true})))
}

async fn delivered(
    State(st): State<AppState>,
    Json(req): Json<FenceReq>,
) -> Result<Json<Value>, GateError> {
    let ok = crate::db::mark_delivered(&st.pool, &req.id, req.fence, &req.relay_id)
        .await
        .map_err(gate)?;
    if !ok {
        stale_audit(&st.pool, &req.id, &req, "delivered").await;
        return Err(GateError::Stale("lease lost or stale fence".into()));
    }
    Ok(Json(json!({"ok": true})))
}

#[derive(Deserialize)]
struct CompleteReq {
    id: String,
    fence: i32,
    relay_id: String,
    tx_hash: String,
}

async fn complete(
    State(st): State<AppState>,
    Json(req): Json<CompleteReq>,
) -> Result<Json<Value>, GateError> {
    if req.tx_hash.trim().is_empty() {
        return Err(GateError::BadRequest("tx_hash required".into()));
    }
    let ok = crate::db::complete(&st.pool, &req.id, req.fence, &req.relay_id, &req.tx_hash)
        .await
        .map_err(gate)?;
    if !ok {
        stale_audit(
            &st.pool,
            &req.id,
            &FenceReq {
                id: req.id.clone(),
                fence: req.fence,
                relay_id: req.relay_id.clone(),
            },
            "complete",
        )
        .await;
        return Err(GateError::Stale(
            "stale fence or submission already settled".into(),
        ));
    }
    Ok(Json(json!({"ok": true, "tx_hash": req.tx_hash})))
}

#[derive(Deserialize)]
struct ReportReq {
    id: String,
    fence: i32,
    relay_id: String,
    /// "retryable" -> 释放租约回 pending；"fatal" -> failed 阻塞通道
    kind: String,
    error: String,
}

async fn report(
    State(st): State<AppState>,
    Json(req): Json<ReportReq>,
) -> Result<Json<Value>, GateError> {
    let ok = match req.kind.as_str() {
        "retryable" => {
            crate::db::report_retryable(&st.pool, &req.id, req.fence, &req.relay_id, &req.error)
                .await
                .map_err(gate)?
        }
        "fatal" => {
            crate::db::report_fatal(&st.pool, &req.id, req.fence, &req.relay_id, &req.error)
                .await
                .map_err(gate)?
        }
        other => {
            return Err(GateError::BadRequest(format!(
                "kind must be retryable|fatal, got {other}"
            )))
        }
    };
    if !ok {
        stale_audit(
            &st.pool,
            &req.id,
            &FenceReq {
                id: req.id.clone(),
                fence: req.fence,
                relay_id: req.relay_id.clone(),
            },
            "report",
        )
        .await;
        return Err(GateError::Stale("lease lost or stale fence".into()));
    }
    Ok(Json(json!({"ok": true, "kind": req.kind})))
}

/// 供 worker 使用的内部 HTTP 客户端。
pub struct InternalClient {
    http: reqwest::Client,
    base: String,
}

impl InternalClient {
    pub fn new(base: impl Into<String>) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .expect("build client"),
            base: base.into(),
        }
    }

    pub async fn claim(&self, relay_id: &str) -> anyhow::Result<Option<ClaimResp>> {
        let resp = self
            .http
            .post(format!("{}/internal/claim", self.base))
            .json(&json!({"relay_id": relay_id}))
            .send()
            .await?;
        if resp.status() == reqwest::StatusCode::NO_CONTENT {
            return Ok(None);
        }
        let resp = resp.error_for_status()?;
        Ok(Some(resp.json::<ClaimResp>().await?))
    }

    async fn post_fence(&self, path: &str, body: Value) -> anyhow::Result<bool> {
        let resp = self
            .http
            .post(format!("{}{}", self.base, path))
            .json(&body)
            .send()
            .await?;
        if resp.status() == reqwest::StatusCode::CONFLICT {
            return Ok(false);
        }
        resp.error_for_status()?;
        Ok(true)
    }

    pub async fn heartbeat(&self, r: &ClaimResp, relay_id: &str) -> anyhow::Result<bool> {
        self.post_fence(
            "/internal/heartbeat",
            json!({"id": r.id, "fence": r.fence, "relay_id": relay_id}),
        )
        .await
    }

    pub async fn delivered(&self, r: &ClaimResp, relay_id: &str) -> anyhow::Result<bool> {
        self.post_fence(
            "/internal/delivered",
            json!({"id": r.id, "fence": r.fence, "relay_id": relay_id}),
        )
        .await
    }

    pub async fn complete(
        &self,
        r: &ClaimResp,
        relay_id: &str,
        tx_hash: &str,
    ) -> anyhow::Result<bool> {
        self.post_fence(
            "/internal/complete",
            json!({"id": r.id, "fence": r.fence, "relay_id": relay_id, "tx_hash": tx_hash}),
        )
        .await
    }

    pub async fn report(
        &self,
        r: &ClaimResp,
        relay_id: &str,
        kind: &str,
        error: &str,
    ) -> anyhow::Result<bool> {
        self.post_fence(
            "/internal/report",
            json!({"id": r.id, "fence": r.fence, "relay_id": relay_id,
                   "kind": kind, "error": error}),
        )
        .await
    }
}
