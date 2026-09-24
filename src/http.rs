use crate::clock::{Clock, ManualClock};
use crate::error::AppError;
use crate::model::*;
use crate::service::WatchdogService;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub svc: Arc<WatchdogService>,
    pub manual_clock: Option<Arc<ManualClock>>,
}

pub fn build_router(state: AppState) -> Router {
    let mut app = Router::new()
        .route("/health", get(health))
        .route("/devices", post(create_device))
        .route("/devices/{device_id}", get(get_status))
        .route("/devices/{device_id}/tasks", post(register_task))
        .route("/devices/{device_id}/heartbeat", post(heartbeat))
        .route("/devices/{device_id}/reset", post(report_reset))
        .route("/devices/{device_id}/tick", post(tick))
        .route("/devices/{device_id}/resets", get(list_resets))
        .route("/devices/{device_id}/events", get(list_events))
        .route(
            "/devices/{device_id}/safe-mode/challenge",
            post(request_challenge),
        )
        .route("/devices/{device_id}/safe-mode/clear", post(clear_safe_mode));

    // Deterministic clock controls exist only when explicitly enabled
    // (WATCHDOG_MANUAL_CLOCK=1): tests and acceptance demos use them,
    // production never does.
    if state.manual_clock.is_some() {
        app = app
            .route("/admin/clock", get(clock_get))
            .route("/admin/clock/advance", post(clock_advance))
            .route("/admin/clock/set", post(clock_set));
    }
    app.with_state(state)
}

async fn health() -> impl IntoResponse {
    Json(serde_json::json!({ "status": "ok" }))
}

async fn create_device(
    State(s): State<AppState>,
    Json(req): Json<CreateDeviceReq>,
) -> impl IntoResponse {
    s.svc.create_device(req).map(|r| (StatusCode::CREATED, Json(r)))
}

async fn get_status(State(s): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    s.svc.status(&id).map(Json)
}

async fn register_task(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<RegisterTaskReq>,
) -> impl IntoResponse {
    s.svc.register_task(&id, req).map(Json)
}

async fn heartbeat(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<HeartbeatReq>,
) -> impl IntoResponse {
    s.svc.heartbeat(&id, req).map(Json)
}

async fn report_reset(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<ReportResetReq>,
) -> impl IntoResponse {
    s.svc.report_reset(&id, req).map(Json)
}

async fn tick(State(s): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    s.svc.tick(&id).map(Json)
}

async fn list_resets(State(s): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    s.svc.list_resets(&id).map(Json)
}

async fn list_events(State(s): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    s.svc.list_events(&id).map(Json)
}

async fn request_challenge(State(s): State<AppState>, Path(id): Path<String>) -> impl IntoResponse {
    s.svc.request_challenge(&id).map(Json)
}

async fn clear_safe_mode(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<ClearReq>,
) -> impl IntoResponse {
    s.svc.clear_safe_mode(&id, req).map(Json)
}

async fn clock_get(State(s): State<AppState>) -> impl IntoResponse {
    let now = s
        .manual_clock
        .as_ref()
        .map(|c| c.now_ms())
        .unwrap_or_default();
    Json(ClockResp { now_ms: now })
}

async fn clock_advance(
    State(s): State<AppState>,
    Json(req): Json<ClockAdvanceReq>,
) -> Result<Json<ClockResp>, AppError> {
    let clock = s
        .manual_clock
        .as_ref()
        .ok_or_else(|| AppError::NotFound("manual clock disabled".into()))?;
    Ok(Json(ClockResp {
        now_ms: clock.advance(req.ms),
    }))
}

async fn clock_set(
    State(s): State<AppState>,
    Json(req): Json<ClockSetReq>,
) -> Result<Json<ClockResp>, AppError> {
    let clock = s
        .manual_clock
        .as_ref()
        .ok_or_else(|| AppError::NotFound("manual clock disabled".into()))?;
    Ok(Json(ClockResp {
        now_ms: clock.set(req.now_ms),
    }))
}
