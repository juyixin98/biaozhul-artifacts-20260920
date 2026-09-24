//! Axum HTTP 路由与处理函数。
//!
//! 接口：
//! - `GET  /healthz`              健康检查
//! - `POST /plan`                只规划不落地：返回完整计划（含全部冲突）
//! - `POST /apply`               规划通过后原子落盘；有冲突返回 409 且不改动目标树

use axum::{
    Json, Router,
    extract::Request,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde_json::json;

use crate::apply;
use crate::model::*;
use crate::plan;

pub fn router() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/plan", post(plan_handler))
        .route("/apply", post(apply_handler))
        .fallback(fallback)
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok", "service": "build-output-merge" }))
}

/// `POST /plan`：规划永远在 HTTP 层成功（200），冲突体现在响应体 `ok=false`；
/// 只有请求本身无法解析 / 不合法才是 4xx。
async fn plan_handler(Json(req): Json<MergeRequest>) -> Response {
    match plan::build_plan(&req) {
        Ok(p) => {
            let ok = p.ok();
            Json(json!({ "ok": ok, "plan": p })).into_response()
        }
        Err(plan::PlanError::BadRequest(msg)) => bad_request(msg),
    }
}

/// `POST /apply`：落盘。冲突 → 409（响应体携带计划）；IO 错误 → 500。
async fn apply_handler(Json(req): Json<MergeRequest>) -> Response {
    // 文件操作是阻塞的，放到阻塞线程池，避免拖住 async runtime。
    let result = tokio::task::spawn_blocking(move || apply::apply(&req))
        .await
        .expect("apply task panicked");
    match result {
        Ok(res) => (StatusCode::OK, Json(json!({ "ok": true, "result": res }))).into_response(),
        Err(apply::ApplyError::BadRequest(msg)) => bad_request(msg),
        Err(apply::ApplyError::Conflict(p)) => (
            StatusCode::CONFLICT,
            Json(json!({ "ok": false, "error": "merge plan has conflicts; target tree left unchanged", "plan": *p })),
        )
            .into_response(),
        Err(apply::ApplyError::Io(e)) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(ErrorResponse {
                error: format!("io error: {e}"),
            }),
        )
            .into_response(),
    }
}

fn bad_request(msg: String) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(ErrorResponse {
            error: format!("bad request: {msg}"),
        }),
    )
        .into_response()
}

async fn fallback(method: Method, req: Request) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(ErrorResponse {
            error: format!(
                "no route for {} {} (try POST /plan or POST /apply)",
                method,
                req.uri().path()
            ),
        }),
    )
        .into_response()
}
