//! Axum HTTP 接口。
//!
//! - `GET  /healthz`：存活探针
//! - `GET  /`：接口说明
//! - `POST /v1/verify`：提交证明信封与期望，返回逐条策略判定
//! - `POST /v1/fixture-sign`：仅用于本地演示的签名辅助端点（用夹具私钥签名）

use std::sync::Arc;

use axum::extract::rejection::JsonRejection;
use axum::extract::{Json, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use serde::Deserialize;
use serde_json::json;

use crate::fixture::FixtureBundle;
use crate::model::Statement;
use crate::verify::{evaluate, VerificationReport, VerifyRequest};

#[derive(Clone)]
pub struct AppState {
    pub fixtures: Arc<FixtureBundle>,
}

pub fn router(fixtures: Arc<FixtureBundle>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/", get(index))
        .route("/v1/verify", post(verify))
        .route("/v1/fixture-sign", post(fixture_sign))
        .with_state(AppState { fixtures })
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

async fn index() -> Json<serde_json::Value> {
    Json(json!({
        "service": "build-provenance-verify",
        "endpoints": {
            "GET /healthz": "存活探针",
            "POST /v1/verify": "验证构建证明，body 为 VerifyRequest",
            "POST /v1/fixture-sign": "夹具辅助：按 builder_id 对 payload 签名，返回信封",
        },
        "verify_request_fields": [
            "envelope{payload{builder_id,source_commit,materials,output},key_id,signature_hex}",
            "expected_source_commit{repo,revision}",
            "expected_materials[ {uri,digest{alg,hex}} ]",
            "expected_output{digest{alg,hex}}",
        ],
    }))
}

async fn verify(
    State(state): State<AppState>,
    parse: Result<Json<VerifyRequest>, JsonRejection>,
) -> Response {
    let Json(req) = match parse {
        Ok(r) => r,
        Err(rej) => return bad_request(rej),
    };
    let report: VerificationReport = evaluate(&state.fixtures.policy, &req);
    Json(report).into_response()
}

#[derive(Deserialize)]
struct FixtureSignRequest {
    builder_id: String,
    payload: Statement,
}

async fn fixture_sign(
    State(state): State<AppState>,
    parse: Result<Json<FixtureSignRequest>, JsonRejection>,
) -> Response {
    let Json(req) = match parse {
        Ok(r) => r,
        Err(rej) => return bad_request(rej),
    };
    if !state.fixtures.secrets.contains_key(&req.builder_id) {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "unknown builder",
                "known_builders": state.fixtures.secrets.keys().collect::<Vec<_>>(),
            })),
        )
            .into_response();
    }
    let envelope = crate::fixture::seal(&state.fixtures, &req.builder_id, req.payload);
    Json(envelope).into_response()
}

/// 请求体不是合法 JSON（或结构不匹配）统一返回 400。
fn bad_request(rej: JsonRejection) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({ "error": "invalid JSON request body", "detail": rej.body_text() })),
    )
        .into_response()
}
