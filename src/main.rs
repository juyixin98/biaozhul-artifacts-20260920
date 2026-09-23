//! HTTP API for the interval-lock service (axum).
//!
//! Endpoints
//! ---------
//! * `POST   /txn`            -> begin a transaction            `{"txn": 1}`
//! * `GET    /txn/:id`        -> transaction view
//! * `POST   /txn/:id/commit` -> commit (release all locks)
//! * `POST   /txn/:id/abort`  -> abort  (release all locks)
//! * `POST   /lock`           -> acquire a lock
//!     body: `{"txn":1,"mode":"shared|exclusive","start":0,"end":10,
//!             "block":false,"timeout_ms":5000}`
//! * `POST   /unlock`         -> release one exactly-matching lock
//!     body: `{"txn":1,"mode":"exclusive","start":0,"end":10}`
//! * `GET    /state`          -> full snapshot: txns, queue, wait-for graph, log
//! * `POST   /reset`          -> wipe everything (test/demo helper)

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use interval_lock::{Interval, LockError, LockManager, LockOutcome, Mode, StateView};
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Duration;

#[derive(Clone)]
struct AppState {
    lm: LockManager,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "interval_lock_server=info,tower_http=info".into()),
        )
        .init();

    let state = AppState { lm: LockManager::new() };
    let app = app(state);

    let addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:3000".to_string());
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("failed to bind");
    tracing::info!("interval-lock-server listening on http://{}", addr);
    axum::serve(listener, app).await.expect("server error");
}

fn app(state: AppState) -> Router {
    Router::new()
        .route("/txn", post(begin_txn))
        .route("/txn/:id", get(get_txn))
        .route("/txn/:id/commit", post(commit_txn))
        .route("/txn/:id/abort", post(abort_txn))
        .route("/lock", post(lock))
        .route("/unlock", post(unlock))
        .route("/state", get(get_state))
        .route("/reset", post(reset))
        .with_state(state)
}

// ---------- helpers ----------

fn err_response(status: StatusCode, e: &LockError) -> Response {
    (status, Json(serde_json::to_value(e).unwrap())).into_response()
}

fn map_err(e: &LockError) -> StatusCode {
    match e {
        LockError::InvalidInterval => StatusCode::BAD_REQUEST,
        LockError::TxnNotFound => StatusCode::NOT_FOUND,
        LockError::TxnNotActive { .. } => StatusCode::CONFLICT,
        LockError::LockNotHeld => StatusCode::NOT_FOUND,
        LockError::AlreadyWaiting => StatusCode::CONFLICT,
        LockError::Timeout => StatusCode::REQUEST_TIMEOUT,
        LockError::Aborted => StatusCode::CONFLICT,
    }
}

fn outcome_json(outcome: &LockOutcome) -> Value {
    serde_json::to_value(outcome).unwrap()
}

// ---------- handlers ----------

async fn begin_txn(State(st): State<AppState>) -> Json<Value> {
    let id = st.lm.begin_txn();
    Json(json!({ "txn": id }))
}

async fn get_txn(State(st): State<AppState>, Path(id): Path<u64>) -> Response {
    let view = st.lm.state();
    match view.txns.iter().find(|t| t.id == id) {
        Some(t) => Json(serde_json::to_value(t).unwrap()).into_response(),
        None => err_response(StatusCode::NOT_FOUND, &LockError::TxnNotFound),
    }
}

async fn commit_txn(State(st): State<AppState>, Path(id): Path<u64>) -> Response {
    match st.lm.commit(id) {
        Ok(state) => Json(json!({ "txn": id, "state": state })).into_response(),
        Err(e) => err_response(map_err(&e), &e),
    }
}

async fn abort_txn(State(st): State<AppState>, Path(id): Path<u64>) -> Response {
    match st.lm.abort(id) {
        Ok(state) => Json(json!({ "txn": id, "state": state })).into_response(),
        Err(e) => err_response(map_err(&e), &e),
    }
}

#[derive(Debug, Deserialize)]
struct LockBody {
    txn: u64,
    mode: Mode,
    start: i64,
    end: i64,
    /// If true, the HTTP request blocks until the lock is granted / the txn is
    /// aborted / `timeout_ms` elapses. Default false (returns `waiting`).
    #[serde(default)]
    block: bool,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

fn default_timeout_ms() -> u64 {
    5000
}

async fn lock(State(st): State<AppState>, Json(body): Json<LockBody>) -> Response {
    let interval = match Interval::new(body.start, body.end) {
        Ok(i) => i,
        Err(e) => return err_response(map_err(&e), &e),
    };
    let result = if body.block {
        st.lm
            .lock_blocking(body.txn, body.mode, interval, Duration::from_millis(body.timeout_ms))
            .await
    } else {
        st.lm.lock(body.txn, body.mode, interval)
    };
    match result {
        Ok(outcome) => {
            let status = match &outcome {
                LockOutcome::Granted => StatusCode::OK,
                LockOutcome::Waiting { .. } => StatusCode::ACCEPTED,
                LockOutcome::Deadlock { .. } => StatusCode::CONFLICT,
            };
            (status, Json(outcome_json(&outcome))).into_response()
        }
        Err(e) => {
            // Distinguish "timed out while blocked" from a real conflict.
            if body.block && e == LockError::Timeout {
                return (
                    StatusCode::REQUEST_TIMEOUT,
                    Json(json!({ "error": "timeout", "detail": "still waiting after timeout_ms" })),
                )
                    .into_response();
            }
            err_response(map_err(&e), &e)
        }
    }
}

#[derive(Debug, Deserialize)]
struct UnlockBody {
    txn: u64,
    mode: Mode,
    start: i64,
    end: i64,
}

async fn unlock(State(st): State<AppState>, Json(body): Json<UnlockBody>) -> Response {
    let interval = match Interval::new(body.start, body.end) {
        Ok(i) => i,
        Err(e) => return err_response(map_err(&e), &e),
    };
    match st.lm.unlock(body.txn, body.mode, interval) {
        Ok(()) => Json(json!({ "status": "released" })).into_response(),
        Err(e) => err_response(map_err(&e), &e),
    }
}

async fn get_state(State(st): State<AppState>) -> Json<StateView> {
    Json(st.lm.state())
}

async fn reset(State(st): State<AppState>) -> Json<Value> {
    st.lm.reset();
    Json(json!({ "status": "reset" }))
}

#[cfg(test)]
mod http_tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Method, Request, StatusCode};
    use serde_json::Value;
    use tower::ServiceExt;

    fn test_lm() -> LockManager {
        LockManager::new()
    }

    /// Issue one request against a fresh Router backed by the same manager.
    async fn call(
        lm: &LockManager,
        method: Method,
        uri: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let app = app(AppState { lm: lm.clone() });
        let mut builder = Request::builder().method(method).uri(uri);
        let body = match body {
            Some(v) => {
                builder = builder.header("content-type", "application/json");
                Body::from(serde_json::to_vec(&v).unwrap())
            }
            None => Body::empty(),
        };
        let resp = app.oneshot(builder.body(body).unwrap()).await.unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let value = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap()
        };
        (status, value)
    }

    #[tokio::test]
    async fn http_full_lifecycle_and_two_txn_deadlock() {
        let lm = test_lm();

        // begin T1, T2
        let (s, v) = call(&lm, Method::POST, "/txn", None).await;
        assert_eq!(s, StatusCode::OK);
        let t1 = v["txn"].as_u64().unwrap();
        let (s, v) = call(&lm, Method::POST, "/txn", None).await;
        assert_eq!(s, StatusCode::OK);
        let t2 = v["txn"].as_u64().unwrap();

        // T1 X[0,10), T2 X[20,30)
        let (s, _) = call(
            &lm,
            Method::POST,
            "/lock",
            Some(json!({"txn": t1, "mode": "exclusive", "start": 0, "end": 10})),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        let (s, _) = call(
            &lm,
            Method::POST,
            "/lock",
            Some(json!({"txn": t2, "mode": "exclusive", "start": 20, "end": 30})),
        )
        .await;
        assert_eq!(s, StatusCode::OK);

        // T2 waits for T1 -> 202
        let (s, v) = call(
            &lm,
            Method::POST,
            "/lock",
            Some(json!({"txn": t2, "mode": "exclusive", "start": 0, "end": 10})),
        )
        .await;
        assert_eq!(s, StatusCode::ACCEPTED);
        assert_eq!(v["status"], "waiting");

        // T1 waits for T2 -> 409 deadlock, victim T2
        let (s, v) = call(
            &lm,
            Method::POST,
            "/lock",
            Some(json!({"txn": t1, "mode": "exclusive", "start": 20, "end": 30})),
        )
        .await;
        assert_eq!(s, StatusCode::CONFLICT);
        assert_eq!(v["status"], "deadlock");
        assert_eq!(v["victim"], t2);

        // T1 got [20,30) after the victim was aborted.
        let (s, v) = call(&lm, Method::GET, &format!("/txn/{}", t1), None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["state"], "active");
        assert!(v["waiting"].is_null());
        let granted: Vec<(i64, i64)> = v["held"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| (h["interval"]["start"].as_i64().unwrap(), h["interval"]["end"].as_i64().unwrap()))
            .collect();
        assert!(granted.contains(&(20, 30)));

        // state shows no wait edges
        let (s, v) = call(&lm, Method::GET, "/state", None).await;
        assert_eq!(s, StatusCode::OK);
        assert!(v["wait_for"].as_array().unwrap().is_empty());

        // T1 commits
        let (s, _) = call(&lm, Method::POST, &format!("/txn/{}/commit", t1), None).await;
        assert_eq!(s, StatusCode::OK);

        // invalid interval -> 400, unknown txn -> 404
        let (s, _) = call(
            &lm,
            Method::POST,
            "/lock",
            Some(json!({"txn": t1, "mode": "shared", "start": 5, "end": 5})),
        )
        .await;
        assert_eq!(s, StatusCode::BAD_REQUEST);
        let (s, _) = call(&lm, Method::POST, "/txn/999/abort", None).await;
        assert_eq!(s, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn http_adjacent_intervals_grant_and_no_false_positive() {
        let lm = test_lm();
        let (_, v) = call(&lm, Method::POST, "/txn", None).await;
        let t1 = v["txn"].as_u64().unwrap();
        let (_, v) = call(&lm, Method::POST, "/txn", None).await;
        let t2 = v["txn"].as_u64().unwrap();
        for (t, a, b) in [(t1, 0, 10), (t2, 10, 20)] {
            let (s, v) = call(
                &lm,
                Method::POST,
                "/lock",
                Some(json!({"txn": t, "mode": "exclusive", "start": a, "end": b})),
            )
            .await;
            assert_eq!(s, StatusCode::OK, "{}", v);
        }
        let (_, v) = call(&lm, Method::GET, "/state", None).await;
        assert!(v["wait_for"].as_array().unwrap().is_empty());
    }
}
