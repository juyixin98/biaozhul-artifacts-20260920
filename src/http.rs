//! HTTP 层：路由、处理器、请求/响应模型。

use crate::clock::InjectedClock;
use crate::error::{AppResult, Error};
use crate::store::Store;
use axum::body::Bytes;
use axum::extract::{FromRequest, Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;

const BODY_LIMIT: usize = 1024 * 1024;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    /// 注入时钟（仅 `--clock injected` 启动时为 Some）
    pub injected: Option<Arc<InjectedClock>>,
    /// 请求未指定 ttl_ms 时的默认预留存活毫秒数
    pub default_ttl_ms: i64,
}

/// JSON 提取器：任何解析失败都归一成 400（而不是 axum 默认的 422/400 混杂）。
pub struct ReqJson<T>(pub T);

#[async_trait]
impl<S, T> FromRequest<S> for ReqJson<T>
where
    S: Send + Sync,
    T: for<'de> Deserialize<'de>,
{
    type Rejection = Error;

    async fn from_request(
        req: axum::extract::Request,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        let bytes = <Bytes as FromRequest<S>>::from_request(req, state)
            .await
            .map_err(|e| Error::bad_request(format!("failed to read body: {e}")))?;
        if bytes.len() > BODY_LIMIT {
            return Err(Error::bad_request(format!(
                "request body too large ({} > {BODY_LIMIT})",
                bytes.len()
            )));
        }
        let value: T = serde_json::from_slice(&bytes)?;
        Ok(ReqJson(value))
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateTenantReq {
    pub tenant_id: String,
    pub quota_bytes: u64,
    pub quota_objects: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReserveReq {
    pub size_bytes: u64,
    pub objects: u64,
    #[serde(default)]
    pub ttl_ms: Option<i64>,
    #[serde(default)]
    pub reservation_id: Option<String>,
    #[serde(default)]
    pub idempotency_key: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdvanceClockReq {
    pub delta_ms: i64,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/tenants", post(create_tenant))
        .route("/tenants/:tenant_id", get(get_tenant))
        .route(
            "/tenants/:tenant_id/reservations",
            post(create_reservation),
        )
        .route(
            "/tenants/:tenant_id/reservations/:reservation_id",
            get(get_reservation),
        )
        .route(
            "/tenants/:tenant_id/reservations/:reservation_id/commit",
            post(commit_reservation),
        )
        .route(
            "/tenants/:tenant_id/reservations/:reservation_id/cancel",
            post(cancel_reservation),
        )
        .route("/admin/clock/advance", post(advance_clock))
        .route("/admin/expire-due", post(expire_due))
        .with_state(state)
}

async fn healthz(State(st): State<AppState>) -> AppResult<impl IntoResponse> {
    Ok(Json(json!({
        "status": "ok",
        "clock_mode": if st.injected.is_some() { "injected" } else { "system" },
        "now_ms": st.store.now_ms(),
    })))
}

async fn create_tenant(
    State(st): State<AppState>,
    ReqJson(req): ReqJson<CreateTenantReq>,
) -> AppResult<Response> {
    if req.tenant_id.is_empty() {
        return Err(Error::bad_request("tenant_id must not be empty"));
    }
    if req.quota_bytes == 0 {
        return Err(Error::bad_request("quota_bytes must be > 0"));
    }
    if req.quota_objects == 0 {
        return Err(Error::bad_request("quota_objects must be > 0"));
    }
    let info = st
        .store
        .create_tenant(&req.tenant_id, req.quota_bytes, req.quota_objects)?;
    Ok((StatusCode::CREATED, Json(json!(info))).into_response())
}

async fn get_tenant(
    State(st): State<AppState>,
    Path(tenant_id): Path<String>,
) -> AppResult<Json<Value>> {
    let info = st.store.tenant_info(&tenant_id)?;
    Ok(Json(json!(info)))
}

async fn create_reservation(
    State(st): State<AppState>,
    Path(tenant_id): Path<String>,
    ReqJson(req): ReqJson<ReserveReq>,
) -> AppResult<Response> {
    let ttl_ms = match req.ttl_ms {
        Some(v) if v > 0 => v,
        Some(_) => return Err(Error::bad_request("ttl_ms must be > 0")),
        None => st.default_ttl_ms,
    };
    if req.reservation_id.as_deref() == Some("") {
        return Err(Error::bad_request("reservation_id must not be empty"));
    }
    if req.idempotency_key.as_deref() == Some("") {
        return Err(Error::bad_request("idempotency_key must not be empty"));
    }
    let (view, created) = st.store.reserve(
        &tenant_id,
        req.reservation_id,
        req.size_bytes,
        req.objects,
        ttl_ms,
        req.idempotency_key,
    )?;
    let status = if created {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(json!({ "reservation": view, "created": created }))).into_response())
}

async fn get_reservation(
    State(st): State<AppState>,
    Path((tenant_id, reservation_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let view = st.store.reservation(&tenant_id, &reservation_id)?;
    Ok(Json(json!(view)))
}

async fn commit_reservation(
    State(st): State<AppState>,
    Path((tenant_id, reservation_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let view = st.store.commit(&tenant_id, &reservation_id)?;
    Ok(Json(json!(view)))
}

async fn cancel_reservation(
    State(st): State<AppState>,
    Path((tenant_id, reservation_id)): Path<(String, String)>,
) -> AppResult<Json<Value>> {
    let (view, released) = st.store.cancel(&tenant_id, &reservation_id)?;
    Ok(Json(json!({ "reservation": view, "released": released })))
}

async fn advance_clock(
    State(st): State<AppState>,
    ReqJson(req): ReqJson<AdvanceClockReq>,
) -> AppResult<Json<Value>> {
    let clock = st.injected.ok_or(Error::ClockNotControllable)?;
    if req.delta_ms < 0 {
        return Err(Error::bad_request("delta_ms must be >= 0"));
    }
    let now = clock.advance(req.delta_ms);
    let expired = st.store.expire_due()?;
    Ok(Json(json!({ "now_ms": now, "expired": expired })))
}

async fn expire_due(State(st): State<AppState>) -> AppResult<Json<Value>> {
    let expired = st.store.expire_due()?;
    Ok(Json(json!({ "expired": expired, "now_ms": st.store.now_ms() })))
}
