//! HTTP API for the watchdog state machine.
//!
//! Endpoints:
//!   GET  /healthz                       liveness probe
//!   GET  /status                        full device + task state
//!   POST /tasks/:task/heartbeat         { "counter": u64 }
//!   POST /feed                          attempt to feed the watchdog
//!   POST /tick                          run one supervision cycle (manual-clock mode)
//!   POST /clock/advance                 { "delta_ms": u64 } (manual-clock mode)
//!   POST /clock/set                     { "now_ms": u64 }   (manual-clock mode)
//!   POST /safe-mode/clear               { "fault_generation": u64 }
//!   GET  /resets                        durable reset journal

use crate::core::{ClearError, FeedError, HeartbeatError, TickOutcome};
use crate::{App, Clock, ClockMode};
use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;

/// Build the application router.
pub fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/status", get(status))
        .route("/tasks/{task}/heartbeat", post(heartbeat))
        .route("/feed", post(feed))
        .route("/tick", post(tick))
        .route("/clock/advance", post(clock_advance))
        .route("/clock/set", post(clock_set))
        .route("/safe-mode/clear", post(clear_safe_mode))
        .route("/resets", get(resets))
        .with_state(app)
}

/// Error envelope shared by every failing endpoint.
struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

impl ApiError {
    fn bad_request(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: message.into(),
        }
    }

    fn conflict(code: &'static str, message: String) -> Self {
        ApiError {
            status: StatusCode::CONFLICT,
            code,
            message,
        }
    }

    fn internal(message: impl std::fmt::Display) -> Self {
        ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal",
            message: message.to_string(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "error": self.code,
            "message": self.message,
        }));
        (self.status, body).into_response()
    }
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "ok": true }))
}

async fn status(State(app): State<Arc<App>>) -> Result<Json<serde_json::Value>, ApiError> {
    let mut wd = app.watchdog.lock().unwrap();
    Ok(Json(json!({
        "clock_mode": match app.clock_mode {
            ClockMode::System => "system",
            ClockMode::Manual => "manual",
        },
        "state": wd.status(),
    })))
}

#[derive(Deserialize)]
struct HeartbeatReq {
    counter: u64,
}

async fn heartbeat(
    State(app): State<Arc<App>>,
    axum::extract::Path(task): axum::extract::Path<String>,
    Json(req): Json<HeartbeatReq>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let mut wd = app.watchdog.lock().unwrap();
    match wd.heartbeat(&task, req.counter) {
        Ok(ok) => Ok(Json(json!({
            "accepted": true,
            "result": ok,
            "note": if ok.progressed {
                "counter advanced: real progress"
            } else {
                "counter unchanged: heartbeat only, not progress"
            },
            "safe_mode": wd.status().safe_mode,
        }))),
        Err(HeartbeatError::UnknownTask { task }) => Err(ApiError::bad_request(format!(
            "unknown task '{task}'; configured tasks are {:?}",
            wd.status().task_progress_required
        ))),
        Err(HeartbeatError::CounterRegression { task, last, got }) => Err(ApiError::conflict(
            "counter_regression",
            format!("task '{task}' counter regressed: last={last}, got={got}"),
        )),
    }
}

async fn feed(State(app): State<Arc<App>>) -> Result<Json<serde_json::Value>, ApiError> {
    let mut wd = app.watchdog.lock().unwrap();
    match wd.feed() {
        Ok(ok) => Ok(Json(json!({ "accepted": true, "result": ok }))),
        Err(FeedError::SafeMode { fault_generation }) => Err(ApiError::conflict(
            "safe_mode",
            format!(
                "device is in safe mode (fault_generation={fault_generation}); \
                 ordinary feeds cannot clear it, a manual clear bound to this generation is required"
            ),
        )),
        Err(FeedError::Stalled { stalled }) => Err(ApiError::conflict(
            "stalled",
            format!(
                "feed rejected: no fresh progress within the window from tasks: {stalled:?}"
            ),
        )),
    }
}

async fn tick(State(app): State<Arc<App>>) -> Result<Json<serde_json::Value>, ApiError> {
    let mut wd = app.watchdog.lock().unwrap();
    let outcome = wd.tick();
    let entered_safe_mode = matches!(
        outcome,
        TickOutcome::Reset { safe_mode: true, .. }
    );
    Ok(Json(json!({
        "outcome": outcome,
        "entered_safe_mode": entered_safe_mode,
    })))
}

#[derive(Deserialize)]
struct AdvanceReq {
    delta_ms: u64,
}

#[derive(Deserialize)]
struct SetClockReq {
    now_ms: u64,
}

fn manual_clock(app: &App) -> Result<&Arc<crate::ManualClock>, ApiError> {
    app.manual_clock
        .as_ref()
        .ok_or_else(|| ApiError::bad_request("clock control requires --clock manual mode"))
}

async fn clock_advance(
    State(app): State<Arc<App>>,
    Json(req): Json<AdvanceReq>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let mc = manual_clock(&app)?.clone();
    mc.advance_ms(req.delta_ms);
    Ok(Json(json!({ "now_ms": mc.now_ms(), "advanced_ms": req.delta_ms })))
}

async fn clock_set(
    State(app): State<Arc<App>>,
    Json(req): Json<SetClockReq>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let mc = manual_clock(&app)?.clone();
    mc.set_ms(req.now_ms);
    Ok(Json(json!({ "now_ms": mc.now_ms() })))
}

#[derive(Deserialize)]
struct ClearReq {
    fault_generation: u64,
}

async fn clear_safe_mode(
    State(app): State<Arc<App>>,
    Json(req): Json<ClearReq>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let mut wd = app.watchdog.lock().unwrap();
    match wd.clear_safe_mode(req.fault_generation) {
        Ok(()) => Ok(Json(json!({
            "cleared": true,
            "fault_generation": req.fault_generation,
        }))),
        Err(ClearError::NotInSafeMode) => Err(ApiError::bad_request("device is not in safe mode")),
        Err(ClearError::StaleGeneration { current }) => Err(ApiError::conflict(
            "stale_generation",
            format!(
                "clear request bound to fault_generation={} is stale; current generation is {current}",
                req.fault_generation
            ),
        )),
    }
}

async fn resets(State(app): State<Arc<App>>) -> Result<Json<serde_json::Value>, ApiError> {
    let wd = app.watchdog.lock().unwrap();
    let log = wd.reset_log().map_err(ApiError::internal)?;
    Ok(Json(json!({ "resets": log })))
}
