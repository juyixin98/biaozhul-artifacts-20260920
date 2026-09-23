//! 基于 Axum 的 HTTP 接口层。
//!
//! 所有状态保存在内存中，进程重启即清空（纯演示/测试用服务）。
//!
//! # 接口
//!
//! | 方法 | 路径 | 说明 |
//! |---|---|---|
//! | `POST` | `/txn/:id/begin` | 开启事务 |
//! | `POST` | `/txn/:id/locks` | 加锁（body 见 [`AcquireReq`]），立即返回授予/等待/死锁 |
//! | `POST` | `/txn/:id/commit` | 提交并释放全部锁 |
//! | `POST` | `/txn/:id/abort` | 中止并释放全部锁 |
//! | `GET`  | `/txn/:id` | 查询事务持锁与等待情况 |
//! | `GET`  | `/txns` | 全部事务及状态 |
//! | `GET`  | `/resources` | 全部资源的持锁/等待队列 |
//! | `GET`  | `/waits` | 当前等待图（边） |
//! | `GET`  | `/health` | 健康检查 |
//!
//! 加锁是**非阻塞** API：不能立即授予时返回 `200` 且 `queued=true`
//! （请求留在 FIFO 等待队列中），由后续其他事务的 commit/abort 自动推进。
//! 若本次入队构成死锁，服务确定性地选择牺牲者中止，并在响应的
//! `deadlock` 字段中给出环与牺牲者；若牺牲者就是调用者，HTTP 状态码为
//! `409 Conflict`（响应体结构相同）。

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::engine::{
    AcquireReport, EdgeView, Engine, EngineError, Mode, ResourceView, TxnView,
};

/// 共享应用状态。
#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<Mutex<Engine>>,
}

/// `/txn/:id/locks` 请求体。
#[derive(Debug, Deserialize)]
pub struct AcquireReq {
    /// 资源 id。
    pub resource: String,
    /// `shared` 或 `exclusive`（也接受 `s`/`x`）。
    pub mode: String,
    /// 区间起点（含）。
    pub start: i64,
    /// 区间终点（不含）。
    pub end: i64,
}

/// 统一错误响应体。
#[derive(Debug, Serialize)]
pub struct ErrorResp {
    pub error: String,
}

/// 加锁响应体。
#[derive(Debug, Serialize)]
pub struct AcquireResp {
    pub txn: String,
    pub resource: String,
    pub mode: String,
    pub start: i64,
    pub end: i64,
    #[serde(flatten)]
    pub report: AcquireReport,
}

fn engine_error_status(err: &EngineError) -> StatusCode {
    match err {
        EngineError::TxnAborted { ..} => StatusCode::CONFLICT,
        EngineError::NotFound(_) => StatusCode::NOT_FOUND,
        EngineError::BadInterval { .. } | EngineError::BadMode(_) => StatusCode::BAD_REQUEST,
    }
}

impl IntoResponse for EngineError {
    fn into_response(self) -> Response {
        let status = engine_error_status(&self);
        (status, Json(ErrorResp { error: self.to_string() })).into_response()
    }
}

/// 构建路由器。
pub fn router(engine: Engine) -> Router {
    let state = AppState { engine: Arc::new(Mutex::new(engine)) };
    Router::new()
        .route("/health", get(health))
        .route("/txns", get(list_txns))
        .route("/resources", get(list_resources))
        .route("/waits", get(list_waits))
        .route("/txn/:id", get(get_txn))
        .route("/txn/:id/begin", post(begin_txn))
        .route("/txn/:id/locks", post(acquire_lock))
        .route("/txn/:id/commit", post(commit_txn))
        .route("/txn/:id/abort", post(abort_txn))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

async fn begin_txn(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, EngineError> {
    let mut eng = state.engine.lock().await;
    // 允许复用已中止事务的 id：先清除 aborted 墓碑再开启。
    eng.cleanup(&id);
    eng.begin(&id)?;
    Ok(Json(serde_json::json!({ "txn": id, "status": "active" })))
}

async fn acquire_lock(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<AcquireReq>,
) -> Result<Response, EngineError> {
    let mode = Mode::parse(&req.mode)?;
    let report = {
        let mut eng = state.engine.lock().await;
        eng.acquire(&id, &req.resource, mode, req.start, req.end)?
    };
    let body = AcquireResp {
        txn: id,
        resource: req.resource,
        mode: mode.as_str().to_string(),
        start: req.start,
        end: req.end,
        report,
    };
    // 调用者本人在牺牲者列表中（被中止）=> 409；其余情况 => 200。
    let self_victim = body
        .report
        .deadlock
        .as_ref()
        .map(|d| d.victims.iter().any(|v| v == &body.txn))
        .unwrap_or(false);
    let status = if self_victim {
        StatusCode::CONFLICT
    } else {
        StatusCode::OK
    };
    Ok((status, Json(body)).into_response())
}

async fn commit_txn(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, EngineError> {
    state.engine.lock().await.commit(&id)?;
    Ok(Json(serde_json::json!({ "txn": id, "status": "committed" })))
}

async fn abort_txn(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, EngineError> {
    state.engine.lock().await.abort(&id)?;
    Ok(Json(serde_json::json!({ "txn": id, "status": "aborted" })))
}

async fn get_txn(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<TxnView>, StatusCode> {
    match state.engine.lock().await.txn_view(&id) {
        Some(v) => Ok(Json(v)),
        None => Err(StatusCode::NOT_FOUND),
    }
}

async fn list_txns(State(state): State<AppState>) -> Json<std::collections::BTreeMap<String, String>> {
    Json(state.engine.lock().await.txns())
}

async fn list_resources(State(state): State<AppState>) -> Json<Vec<ResourceView>> {
    Json(state.engine.lock().await.resources_view())
}

async fn list_waits(State(state): State<AppState>) -> Json<Vec<EdgeView>> {
    Json(state.engine.lock().await.build_waits())
}
