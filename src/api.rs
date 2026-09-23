//! Axum HTTP API（纯后端，无前端）。
//!
//! 路由：
//!   GET  /health                        健康检查
//!   GET  /v1/metering/versions          计量版本表
//!   GET  /v1/metering/versions/:ver     单个版本
//!   POST /v1/transactions               提交一次沙箱执行（成功才发布状态）
//!   GET  /v1/state                      查询提交态（?key= 单键，否则列出全部键）
//!   GET  /v1/journal                    最近执行日志（?limit=）
//!   GET  /v1/samples                    样例清单
//!   GET  /v1/samples/:name              下载样例 wasm 字节
//!
//! 所有失败都通过结构化 JSON 如实返回（termination/error），HTTP 状态码仅表示
//! 「请求是否被受理」：执行被终止（燃料/越界/陷阱）是 200 + status=terminated，
//! 因为沙箱行为本身完全符合预期；参数/模块格式错误才是 4xx。

use std::sync::Arc;

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::engine::{Engine, ExecRequest, ExecResponse};
use crate::state::{CommittedState, Journal};

#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<Engine>,
    pub state: Arc<CommittedState>,
    pub journal: Arc<Journal>,
}

pub fn router(app: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/metering/versions", get(list_versions))
        .route("/v1/metering/versions/:ver", get(get_version))
        .route("/v1/transactions", axum::routing::post(execute))
        .route("/v1/state", get(get_state))
        .route("/v1/journal", get(get_journal))
        .route("/v1/samples", get(list_samples))
        .route("/v1/samples/:name", get(get_sample))
        .with_state(app)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "versioned-gas-meter" }))
}

async fn list_versions(State(app): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "current": app.engine.registry().current_version(),
        "versions": app.engine.registry().all(),
    }))
}

async fn get_version(
    State(app): State<AppState>,
    Path(ver): Path<u32>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let s = app
        .engine
        .registry()
        .get(ver)
        .ok_or_else(|| ApiError::not_found(format!("计量版本 {ver} 不存在")))?;
    Ok(Json(serde_json::to_value(s).unwrap()))
}

// ---------------------------------------------------------------------------
// 执行
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ExecuteBody {
    /// 模块来源二选一：module_base64（标准 base64）或 module_ref（如 "sample:finite_loop"）。
    pub module_base64: Option<String>,
    pub module_ref: Option<String>,
    #[serde(default)]
    pub input_base64: String,
    #[serde(default = "default_version")]
    pub metering_version: u32,
    pub fuel_limit: Option<u64>,
    pub idempotency_key: Option<String>,
}

fn default_version() -> u32 {
    1
}

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: String,
    pub code: String,
}

pub struct ApiError {
    pub status: StatusCode,
    pub message: String,
    pub code: String,
}

impl ApiError {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: msg.into(),
            code: "bad_request".into(),
        }
    }
    pub fn not_found(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message: msg.into(),
            code: "not_found".into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(ErrorBody {
            error: self.message,
            code: self.code,
        });
        (self.status, body).into_response()
    }
}

fn resolve_module(body: &ExecuteBody) -> Result<Vec<u8>, ApiError> {
    match (&body.module_base64, &body.module_ref) {
        (Some(b64), _) => crate::base64::decode(b64)
            .ok_or_else(|| ApiError::bad_request("module_base64 不是合法 base64")),
        (None, Some(r#ref)) => {
            let name = r#ref
                .strip_prefix("sample:")
                .ok_or_else(|| ApiError::bad_request("module_ref 目前仅支持 sample:<name> 形式"))?;
            Ok(crate::samples::by_name(name)
                .ok_or_else(|| ApiError::bad_request(format!("未知样例 {name}")))?
                .wasm
                .to_vec())
        }
        (None, None) => Err(ApiError::bad_request(
            "必须提供 module_base64 或 module_ref",
        )),
    }
}

async fn execute(
    State(app): State<AppState>,
    body: Json<ExecuteBody>,
) -> Result<Json<ExecResponse>, ApiError> {
    let module = resolve_module(&body)?;
    let input = if body.input_base64.is_empty() {
        Vec::new()
    } else {
        crate::base64::decode(&body.input_base64)
            .ok_or_else(|| ApiError::bad_request("input_base64 不是合法 base64"))?
    };

    let req = ExecRequest {
        module,
        input,
        metering_version: body.metering_version,
        fuel_limit_override: body.fuel_limit,
        idempotency_key: body.idempotency_key.clone(),
    };

    // WASM 执行是同步且 CPU 密集的，放到阻塞线程池，避免卡住 Axum 异步运行时。
    let engine = app.engine.clone();
    let resp = tokio::task::spawn_blocking(move || engine.execute(req))
        .await
        .map_err(|e| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: format!("执行任务异常：{e}"),
            code: "host_error".into(),
        })?;
    Ok(Json(resp))
}

// ---------------------------------------------------------------------------
// 状态 / 日志 / 样例
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct StateQuery {
    key: Option<String>,
}

async fn get_state(
    State(app): State<AppState>,
    Query(q): Query<StateQuery>,
) -> Json<serde_json::Value> {
    let (seq, kv) = app.state.snapshot();
    match q.key {
        Some(key) => match kv.get(&key) {
            Some(v) => Json(serde_json::json!({
                "state_seq": seq,
                "key": key,
                "value_base64": crate::base64::encode(v),
                "size": v.len(),
            })),
            None => Json(serde_json::json!({
                "state_seq": seq, "key": key, "found": false,
            })),
        },
        None => {
            let keys: std::collections::BTreeMap<String, usize> =
                kv.iter().map(|(k, v)| (k.clone(), v.len())).collect();
            Json(serde_json::json!({ "state_seq": seq, "keys": keys }))
        }
    }
}

#[derive(Debug, Deserialize)]
struct JournalQuery {
    #[serde(default = "default_limit")]
    limit: usize,
}
fn default_limit() -> usize {
    20
}

async fn get_journal(
    State(app): State<AppState>,
    Query(q): Query<JournalQuery>,
) -> Json<serde_json::Value> {
    let limit = q.limit.clamp(1, 200);
    Json(serde_json::json!({ "entries": app.journal.list(limit) }))
}

async fn list_samples() -> Json<serde_json::Value> {
    let samples: Vec<_> = crate::samples::samples()
        .into_iter()
        .map(|s| {
            serde_json::json!({
                "name": s.name,
                "size": s.wasm.len(),
                "description": s.description,
                "input_hint": s.input_hint,
                "download": format!("/v1/samples/{}", s.name),
            })
        })
        .collect();
    Json(serde_json::json!({ "samples": samples }))
}

async fn get_sample(Path(name): Path<String>) -> Result<Response<Body>, ApiError> {
    let sample = crate::samples::by_name(&name)
        .ok_or_else(|| ApiError::not_found(format!("样例 {name} 不存在")))?;
    Response::builder()
        .header(header::CONTENT_TYPE, "application/wasm")
        .header(
            header::CONTENT_DISPOSITION,
            format!("attachment; filename=\"{}.wasm\"", sample.name),
        )
        .body(Body::from(sample.wasm.to_vec()))
        .map_err(|e| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: e.to_string(),
            code: "host_error".into(),
        })
}
