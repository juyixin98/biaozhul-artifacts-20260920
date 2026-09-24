//! HTTP 层（Axum 0.8）：
//! - `GET  /healthz` 健康检查
//! - `POST /plan`    计算增量构建方案（错误 JSON：400 校验失败 / 422 环检测）

use axum::extract::rejection::JsonRejection;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use crate::model::{PlanError, PlanRequest};
use crate::planner;

#[derive(Clone)]
struct AppState {
    // 当前无共享状态；保留以便将来接入持久缓存
    _placeholder: (),
}

pub fn router() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/plan", post(plan))
        .with_state(AppState { _placeholder: () })
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok", "service": "incremental-build-planner" }))
}

async fn plan(
    State(_): State<AppState>,
    body: Result<Json<PlanRequest>, JsonRejection>,
) -> Response {
    let Json(req) = match body {
        Ok(v) => v,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "invalid_json",
                    "message": e.body_text(),
                })),
            )
                .into_response();
        }
    };

    match planner::analyze(&req) {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(err) => {
            let status = match &err {
                PlanError::InvalidRequest { .. } => StatusCode::BAD_REQUEST,
                PlanError::CycleDetected { .. } => StatusCode::UNPROCESSABLE_ENTITY,
            };
            (status, Json(err)).into_response()
        }
    }
}
