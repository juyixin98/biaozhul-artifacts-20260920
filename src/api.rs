//! Axum HTTP API（纯后端，无前端）。
//!
//! 路由：
//!   GET  /healthz                 存活检查
//!   GET  /v1/versions             计量版本与计价规则
//!   GET  /v1/samples              内置样例列表
//!   GET  /v1/samples/:id/wasm     下载样例 wasm（sha256guest 为预编译产物）
//!   POST /v1/execute              执行一次沙箱调用
//!   GET  /v1/state                查看已提交的宿主 KV（hex 值）
//!
//! 所有计算都在请求处理线程内离线完成；服务本身不向模块提供网络能力。

use std::sync::Arc;

use axum::{
    extract::{Path, State as AxumState},
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::receipt::Receipt;
use crate::sandbox::{
    execute, Engines, ExecRequest, DEFAULT_FUEL_LIMIT, DEFAULT_MEMORY_LIMIT, DEFAULT_TABLE_LIMIT,
};
use crate::state::HostState;
use crate::versions::MeteringVersion;

#[derive(Clone)]
pub struct AppState {
    pub engines: Arc<Engines>,
    pub host: Arc<HostState>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/versions", get(versions))
        .route("/v1/samples", get(list_samples))
        .route("/v1/samples/{id}/wasm", get(sample_wasm))
        .route("/v1/execute", post(execute_handler))
        .route("/v1/state", get(get_state))
        .with_state(state)
}

async fn healthz(AxumState(s): AxumState<AppState>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "offline_sandbox": true,
        "network_access_for_modules": false,
        "filesystem_access_for_modules": false,
        "committed_kv_entries": s.host.len(),
    }))
}

async fn versions() -> Json<serde_json::Value> {
    Json(json!({
        "versions": [
            MeteringVersion::V1.rules_summary(),
            MeteringVersion::V2.rules_summary(),
        ],
        "disclaimer":
            "fuel is Wasmtime's abstract metering unit; it is intentionally NOT \
             presented as on-chain gas and no gas equivalence is claimed",
    }))
}

async fn list_samples() -> Json<serde_json::Value> {
    Json(json!({
        "samples": crate::samples::SAMPLES.iter().map(|s| json!({
            "id": s.id,
            "description": s.description,
        })).collect::<Vec<_>>()
    }))
}

async fn sample_wasm(Path(id): Path<String>) -> Result<(StatusCode, Vec<u8>), ApiError> {
    let sample = crate::samples::by_id(&id)
        .ok_or_else(|| ApiError::not_found(format!("unknown sample: {id}")))?;
    let wasm = crate::samples::compile(sample).map_err(ApiError::bad_request)?;
    Ok((StatusCode::OK, wasm))
}

async fn get_state(AxumState(s): AxumState<AppState>) -> impl IntoResponse {
    let snapshot = s.host.current();
    let entries: serde_json::Value = snapshot
        .iter()
        .map(|(k, v)| json!({"key": k, "value_hex": hex::encode(v)}))
        .collect();
    Json(json!({ "entries": entries }))
}

#[derive(Debug, Deserialize)]
pub struct ExecuteBody {
    /// 二选一：sample_id（内置样例）或 module_base64（自定义 wasm）。
    pub sample_id: Option<String>,
    pub module_base64: Option<String>,
    /// 输入：base64 原始字节，或 utf8 文本（优先 base64）。
    pub input_base64: Option<String>,
    pub input_text: Option<String>,
    /// 计量版本，默认 1。
    pub metering_version: Option<u32>,
    pub fuel_limit: Option<u64>,
    pub memory_limit_bytes: Option<u64>,
    pub table_limit_elements: Option<u32>,
}

#[derive(Debug, Serialize)]
pub struct ExecuteResponse {
    pub receipt: Receipt,
    pub output_utf8: Option<String>,
    pub fuel_is_not_gas_notice: String,
}

async fn execute_handler(
    AxumState(s): AxumState<AppState>,
    Json(body): Json<ExecuteBody>,
) -> Result<Json<ExecuteResponse>, ApiError> {
    // 模块来源
    let wasm = match (body.sample_id.as_deref(), body.module_base64.as_deref()) {
        (Some(id), _) => {
            let sample = crate::samples::by_id(id)
                .ok_or_else(|| ApiError::not_found(format!("unknown sample: {id}")))?;
            crate::samples::compile(sample).map_err(ApiError::bad_request)?
        }
        (None, Some(b64)) => base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .map_err(|e| ApiError::bad_request(format!("invalid module_base64: {e}")))?,
        (None, None) => {
            return Err(ApiError::bad_request(
                "either sample_id or module_base64 is required",
            ))
        }
    };

    // 输入
    let input = match (body.input_base64, body.input_text) {
        (Some(b64), _) => base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .map_err(|e| ApiError::bad_request(format!("invalid input_base64: {e}")))?,
        (None, Some(text)) => text.into_bytes(),
        (None, None) => Vec::new(),
    };

    let version = match body.metering_version.unwrap_or(1) {
        1 => MeteringVersion::V1,
        2 => MeteringVersion::V2,
        other => {
            return Err(ApiError::bad_request(format!(
                "unsupported metering_version: {other} (supported: 1, 2)"
            )))
        }
    };

    let mut req = ExecRequest::new(wasm, input, version);
    if let Some(f) = body.fuel_limit {
        if f == 0 {
            return Err(ApiError::bad_request("fuel_limit must be > 0"));
        }
        req.fuel_limit = f;
    }
    if let Some(m) = body.memory_limit_bytes {
        if m < 65536 {
            return Err(ApiError::bad_request(
                "memory_limit_bytes must be >= 65536 (one wasm page)",
            ));
        }
        req.memory_limit_bytes = m;
    }
    if let Some(t) = body.table_limit_elements {
        req.table_limit_elements = t;
    }

    // 沙箱执行是 CPU 密集的同步工作；放到 blocking 线程池，避免阻塞运行时。
    let engines = Arc::clone(&s.engines);
    let host = Arc::clone(&s.host);
    let receipt = tokio::task::spawn_blocking(move || execute(&engines, &host, req))
        .await
        .map_err(|e| ApiError::internal(format!("join error: {e}")))?
        .map_err(|e| {
            // 保留完整错误链（如“白名单拒绝”的具体原因）
            ApiError::bad_request(format!("{e:#}"))
        })?;

    let output_bytes = base64::engine::general_purpose::STANDARD
        .decode(&receipt.output_base64)
        .unwrap_or_default();
    let output_utf8 = String::from_utf8(output_bytes).ok();

    Ok(Json(ExecuteResponse {
        receipt,
        output_utf8,
        fuel_is_not_gas_notice:
            "fuel is Wasmtime abstract metering for deterministic resource accounting, \
             not blockchain gas; no equivalence is claimed."
                .to_string(),
    }))
}

/// 统一错误响应。模块的陷阱/燃料耗尽不是 HTTP 错误（会在收据里体现）；
/// 只有请求本身非法或沙箱装配失败才走这里。
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn bad_request(msg: impl std::fmt::Display) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: msg.to_string(),
        }
    }

    fn not_found(msg: impl std::fmt::Display) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message: msg.to_string(),
        }
    }

    fn internal(msg: impl std::fmt::Display) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: msg.to_string(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        let body = Json(json!({
            "error": self.message,
            "note": "wasm traps, fuel exhaustion and aborts are reported inside the receipt \
                     of a 200 response, not as HTTP errors",
        }));
        (self.status, body).into_response()
    }
}

/// 编译期默认值（供文档/响应引用）。
pub fn default_limits_json() -> serde_json::Value {
    json!({
        "fuel_limit": DEFAULT_FUEL_LIMIT,
        "memory_limit_bytes": DEFAULT_MEMORY_LIMIT,
        "table_limit_elements": DEFAULT_TABLE_LIMIT,
    })
}
