//! HTTP 接口（Axum）。所有处理器只做参数解析与状态码映射，业务在 [`crate::store`]。

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::clock::Clock;
use crate::store::{ReservationView, Store, StoreError, Usage};

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    pub clock: Arc<dyn Clock>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/tenants/{tenant_id}", put(create_tenant))
        .route("/tenants/{tenant_id}/usage", get(get_usage))
        .route("/tenants/{tenant_id}/reservations", post(create_reservation))
        .route("/reservations/{id}", get(get_reservation))
        .route("/reservations/{id}/commit", post(commit))
        .route("/reservations/{id}/cancel", post(cancel))
        .route("/admin/sweep", post(sweep))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

#[derive(Deserialize)]
struct CreateTenantReq {
    byte_quota: i64,
    object_quota: i64,
}

async fn create_tenant(
    State(s): State<AppState>,
    Path(tenant_id): Path<String>,
    Json(req): Json<CreateTenantReq>,
) -> Result<StatusCode, ApiError> {
    let now = s.clock.now_ms();
    s.store
        .create_tenant(&tenant_id, req.byte_quota, req.object_quota, now)
        .map_err(ApiError::from)?;
    Ok(StatusCode::CREATED)
}

#[derive(Serialize)]
struct UsageResp {
    #[serde(flatten)]
    usage: Usage,
    total_bytes: i64,
    total_objects: i64,
}

async fn get_usage(
    State(s): State<AppState>,
    Path(tenant_id): Path<String>,
) -> Result<Json<UsageResp>, ApiError> {
    let usage = s.store.usage(s.clock.as_ref(), &tenant_id)?;
    let total_bytes = usage.total_bytes();
    let total_objects = usage.total_objects();
    Ok(Json(UsageResp { usage, total_bytes, total_objects }))
}

#[derive(Deserialize)]
struct ReserveReq {
    /// 本次预留字节数
    byte_size: i64,
    /// 本次预留对象（块）数
    object_count: i64,
    /// 预留存活时长，毫秒；超时后预留失效并释放额度
    ttl_ms: i64,
}

#[derive(Serialize)]
struct ReserveResp {
    reservation_id: String,
    expires_at: i64,
}

async fn create_reservation(
    State(s): State<AppState>,
    Path(tenant_id): Path<String>,
    Json(req): Json<ReserveReq>,
) -> Result<(StatusCode, Json<ReserveResp>), ApiError> {
    let (id, expires_at) = s.store.reserve(
        s.clock.as_ref(),
        &tenant_id,
        req.byte_size,
        req.object_count,
        req.ttl_ms,
    )?;
    Ok((StatusCode::CREATED, Json(ReserveResp { reservation_id: id, expires_at })))
}

async fn get_reservation(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<ReservationView>, ApiError> {
    Ok(Json(s.store.get_reservation(&id)?))
}

#[derive(Serialize)]
struct StatusResp {
    reservation_id: String,
    status: String,
}

async fn commit(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<StatusResp>, ApiError> {
    s.store.commit(s.clock.as_ref(), &id)?;
    Ok(Json(StatusResp { reservation_id: id, status: "committed".to_string() }))
}

async fn cancel(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<StatusResp>, ApiError> {
    let st = s.store.cancel(s.clock.as_ref(), &id)?;
    Ok(Json(StatusResp { reservation_id: id, status: st.as_str().to_string() }))
}

#[derive(Serialize)]
struct SweepResp {
    expired_count: usize,
}

async fn sweep(State(s): State<AppState>) -> Result<Json<SweepResp>, ApiError> {
    let n = s.store.sweep_expired(s.clock.as_ref())?;
    Ok(Json(SweepResp { expired_count: n }))
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail: Option<serde_json::Value>,
}

struct ApiError {
    status: StatusCode,
    body: ErrorBody,
}

impl ApiError {
    fn new(status: StatusCode, error: &str) -> Self {
        Self { status, body: ErrorBody { error: error.to_string(), detail: None } }
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::QuotaExceeded {
                bytes_used,
                bytes_wanted,
                bytes_limit,
                objects_used,
                objects_wanted,
                objects_limit,
            } => ApiError {
                status: StatusCode::CONFLICT,
                body: ErrorBody {
                    error: "quota_exceeded".into(),
                    detail: Some(serde_json::json!({
                        "bytes":   { "used": bytes_used, "requested": bytes_wanted, "limit": bytes_limit },
                        "objects": { "used": objects_used, "requested": objects_wanted, "limit": objects_limit },
                    })),
                },
            },
            StoreError::NotFound => ApiError::new(StatusCode::NOT_FOUND, "not_found"),
            StoreError::Expired => ApiError::new(StatusCode::GONE, "reservation_expired"),
            StoreError::Conflict { .. } => ApiError::new(StatusCode::CONFLICT, "illegal_transition"),
            StoreError::TenantNotFound => ApiError::new(StatusCode::NOT_FOUND, "tenant_not_found"),
            StoreError::TenantExists => ApiError::new(StatusCode::CONFLICT, "tenant_exists"),
            StoreError::Db(_) => ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}
