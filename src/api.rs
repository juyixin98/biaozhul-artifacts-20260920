//! Axum HTTP 接口层。

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::Json,
    routing::{get, post},
    Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;

use crate::lock_manager::{AcquireError, BeginError, LockManager, LockMode, TxError, TxId};

pub fn router(mgr: Arc<LockManager>) -> Router {
    Router::new()
        .route("/tx", post(begin_tx))
        .route("/tx/{id}/locks", post(acquire_lock))
        .route("/tx/{id}/commit", post(commit_tx))
        .route("/tx/{id}/abort", post(abort_tx))
        .route("/tx/{id}", get(tx_status))
        .route("/graph", get(graph))
        .route("/metrics", get(metrics))
        .route("/report", get(report))
        .with_state(mgr)
}

#[derive(Debug, Deserialize)]
struct AcquireReq {
    resource: String,
    mode: LockMode,
    /// 可选：本次锁等待超时（毫秒），缺省用服务默认
    timeout_ms: Option<u64>,
}

fn err_json(status: StatusCode, err: &str, detail: String) -> (StatusCode, Json<Value>) {
    (status, Json(json!({ "error": err, "detail": detail })))
}

fn acquire_err(e: AcquireError) -> (StatusCode, Json<Value>) {
    match e {
        AcquireError::Deadlock(d) => err_json(StatusCode::CONFLICT, "deadlock_victim", d),
        AcquireError::Timeout(d) => err_json(StatusCode::REQUEST_TIMEOUT, "lock_timeout", d),
        AcquireError::Aborted(d) => err_json(StatusCode::CONFLICT, "aborted", d),
        AcquireError::TxNotActive(d) => err_json(StatusCode::GONE, "tx_not_active", d),
        AcquireError::AlreadyWaiting(d) => err_json(StatusCode::CONFLICT, "already_waiting", d),
        AcquireError::GraphFull(d) => err_json(StatusCode::SERVICE_UNAVAILABLE, "wait_graph_full", d),
    }
}

async fn begin_tx(State(m): State<Arc<LockManager>>) -> (StatusCode, Json<Value>) {
    match m.begin() {
        Ok(id) => (StatusCode::OK, Json(json!({ "tx_id": id }))),
        Err(BeginError::GraphFull(d)) => {
            err_json(StatusCode::SERVICE_UNAVAILABLE, "wait_graph_full", d)
        }
    }
}

async fn acquire_lock(
    State(m): State<Arc<LockManager>>,
    Path(id): Path<TxId>,
    Json(req): Json<AcquireReq>,
) -> (StatusCode, Json<Value>) {
    let timeout = req.timeout_ms.map(Duration::from_millis);
    match m.acquire(id, &req.resource, req.mode, timeout).await {
        Ok(()) => (
            StatusCode::OK,
            Json(json!({ "status": "granted", "tx": id, "resource": req.resource, "mode": req.mode })),
        ),
        Err(e) => acquire_err(e),
    }
}

async fn commit_tx(
    State(m): State<Arc<LockManager>>,
    Path(id): Path<TxId>,
) -> (StatusCode, Json<Value>) {
    match m.commit(id) {
        Ok(()) => (StatusCode::OK, Json(json!({ "status": "committed", "tx": id }))),
        Err(TxError::NotActive(d)) => err_json(StatusCode::GONE, "tx_not_active", d),
    }
}

async fn abort_tx(
    State(m): State<Arc<LockManager>>,
    Path(id): Path<TxId>,
) -> (StatusCode, Json<Value>) {
    match m.abort(id) {
        Ok(()) => (StatusCode::OK, Json(json!({ "status": "aborted", "tx": id }))),
        Err(TxError::NotActive(d)) => err_json(StatusCode::GONE, "tx_not_active", d),
    }
}

async fn tx_status(
    State(m): State<Arc<LockManager>>,
    Path(id): Path<TxId>,
) -> (StatusCode, Json<Value>) {
    match m.tx_status(id) {
        Some(s) => (StatusCode::OK, Json(json!({ "tx": id, "status": s }))),
        None => err_json(StatusCode::NOT_FOUND, "tx_not_found", format!("tx {id} not found")),
    }
}

async fn graph(State(m): State<Arc<LockManager>>) -> Json<Value> {
    Json(json!({ "wait_for_edges": m.graph_edges() }))
}

async fn metrics(State(m): State<Arc<LockManager>>) -> Json<Value> {
    Json(json!(m.metrics()))
}

async fn report(State(m): State<Arc<LockManager>>) -> String {
    let met = m.metrics();
    let mut s = String::new();
    s.push_str("=== Lock Manager Report ===\n");
    s.push_str(&format!(
        "locks granted: {} (waited: {})\n",
        met.locks_granted, met.locks_waited
    ));
    s.push_str(&format!("commits: {}\n", met.commits));
    s.push_str(&format!(
        "deadlocks detected: {}, deadlock aborts: {}\n",
        met.deadlocks_detected, met.deadlock_aborts
    ));
    s.push_str(&format!(
        "timeout aborts: {} (false-kill: {}, timeout-while-on-cycle: {})\n",
        met.timeout_aborts, met.timeout_aborts_false_kill, met.timeout_aborts_in_cycle
    ));
    s.push_str(&format!("user aborts: {}\n", met.user_aborts));
    s.push_str(&format!(
        "wait chain length: samples={}, avg={:.2}, max={}\n",
        met.wait_chain_samples, met.wait_chain_avg, met.wait_chain_max
    ));
    s.push_str(&format!(
        "wait-for graph: edges={}/{}, full rejections={}\n",
        met.wait_graph_edges, met.wait_graph_max_edges, met.wait_graph_full_rejections
    ));
    s.push_str(&format!(
        "current: holders={}, waiters={}, active_txs={}\n",
        met.current_holders, met.current_waiters, met.active_txs
    ));
    if met.deadlock_log.is_empty() {
        s.push_str("deadlock log: (empty)\n");
    } else {
        s.push_str("deadlock log:\n");
        for line in &met.deadlock_log {
            s.push_str(&format!("  - {line}\n"));
        }
    }
    s
}
