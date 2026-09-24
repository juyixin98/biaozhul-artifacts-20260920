//! HTTP 层：把纯函数引擎暴露为 Axum 接口。
//!
//! - `POST /plan`     计算增量构建计划（静态错误/环 -> 422，携带可定位细节）
//! - `POST /validate` 只校验依赖图并返回拓扑序 / 环
//! - `GET  /health`   健康检查

use axum::{
    extract::rejection::JsonRejection,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::engine::{self, PlanRequest, PlanResponse};
use crate::graph::Graph;

pub fn app() -> Router {
    Router::new()
        .route("/plan", post(plan))
        .route("/validate", post(validate))
        .route("/health", get(health))
}

/// 统一错误：请求体解析失败 -> 400；图非法/有环 -> 422。
struct ApiError {
    status: StatusCode,
    body: serde_json::Value,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

impl From<JsonRejection> for ApiError {
    fn from(rej: JsonRejection) -> Self {
        ApiError {
            status: rej.status(),
            body: serde_json::json!({ "error": rej.body_text() }),
        }
    }
}

impl From<engine::PlanError> for ApiError {
    fn from(e: engine::PlanError) -> Self {
        ApiError {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            body: serde_json::to_value(e).unwrap_or(serde_json::json!({
                "error": "planning failed",
            })),
        }
    }
}

async fn plan(Json(req): Json<PlanRequest>) -> Result<Json<PlanResponse>, ApiError> {
    Ok(Json(engine::plan(req)?))
}

#[derive(Debug, Deserialize)]
struct ValidateRequest {
    graph: Graph,
}

#[derive(Debug, Serialize)]
struct ValidateResponse {
    valid: bool,
    /// 图无环时给出全部节点的拓扑序（同层按 id 字典序）。
    topo_order: Vec<String>,
    cycles: Vec<crate::graph::Cycle>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    issues: Vec<crate::graph::GraphIssue>,
}

async fn validate(Json(req): Json<ValidateRequest>) -> Result<Json<ValidateResponse>, ApiError> {
    match req.graph.compile() {
        Ok(g) => {
            let cycles = g.find_cycles();
            if cycles.is_empty() {
                let (topo, _) = g.topo_sort();
                Ok(Json(ValidateResponse {
                    valid: true,
                    topo_order: topo,
                    cycles,
                    issues: Vec::new(),
                }))
            } else {
                Ok(Json(ValidateResponse {
                    valid: false,
                    topo_order: Vec::new(),
                    cycles,
                    issues: Vec::new(),
                }))
            }
        }
        Err(issues) => Ok(Json(ValidateResponse {
            valid: false,
            topo_order: Vec::new(),
            cycles: Vec::new(),
            issues,
        })),
    }
}

#[derive(Debug, Serialize)]
struct Health {
    status: &'static str,
    service: &'static str,
    version: &'static str,
}

async fn health() -> Json<Health> {
    Json(Health {
        status: "ok",
        service: "incr-build-planner",
        version: env!("CARGO_PKG_VERSION"),
    })
}
