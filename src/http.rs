//! Axum HTTP 层：鉴权、请求解析、路由。业务规则全部在 [`crate::arbiter`]。

use std::collections::HashMap;
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;

use crate::arbiter::Arbiter;
use crate::crypto::verify_hex;
use crate::model::reason::VALIDATION;
use crate::model::*;

#[derive(Clone)]
pub struct Keys {
    pub autonomous: Arc<Vec<u8>>,
    pub remote: Arc<Vec<u8>>,
    pub estop: Arc<Vec<u8>>,
    pub admin_token: Option<Arc<String>>,
}

#[derive(Clone)]
pub struct AppState {
    pub arbiter: Arc<Arbiter>,
    pub keys: Keys,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/sources/{source}/commands", post(post_command))
        .route("/v1/sources/{source}/leases/release", post(post_release))
        .route("/v1/estop", post(post_estop))
        .route("/v1/decisions/evaluate", post(post_evaluate))
        .route("/v1/decision", get(get_decision))
        .route("/v1/decisions", get(list_decisions))
        .route("/v1/ingest-events", get(list_ingest_events))
        .route("/v1/state", get(get_state))
        .route("/admin/reset", post(post_admin_reset))
        .with_state(state)
}

// ---------- 辅助 ----------

fn error(status: StatusCode, error: &str, reason: &str, detail: Option<String>) -> Response {
    (status, Json(ApiError {
        error: error.to_string(),
        reason: reason.to_string(),
        detail,
    }))
        .into_response()
}

fn map_arbiter_err(e: (u16, ApiError)) -> Response {
    let status = StatusCode::from_u16(e.0).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(e.1)).into_response()
}

fn sim_at_header(headers: &HeaderMap) -> Result<Option<i64>, Box<Response>> {
    match headers.get("x-sim-at") {
        None => Ok(None),
        Some(v) => {
            let s = v.to_str().unwrap_or("");
            match s.parse::<i64>() {
                Ok(n) => Ok(Some(n)),
                Err(_) => Err(Box::new(error(
                    StatusCode::BAD_REQUEST,
                    "bad_request",
                    "invalid_x_sim_at",
                    Some(format!("X-Sim-At must be integer unix-ms, got {s:?}")),
                ))),
            }
        }
    }
}

fn wall_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after epoch")
        .as_millis() as i64
}

/// 对写入请求做 HMAC 校验。key_id = autonomous|remote|estop。
fn authenticate(
    state: &AppState,
    key_id: &str,
    headers: &HeaderMap,
    path: &str,
    method: &str,
    body: &[u8],
) -> Result<(), Box<Response>> {
    let key: &[u8] = match key_id {
        "autonomous" => &state.keys.autonomous,
        "remote" => &state.keys.remote,
        "estop" => &state.keys.estop,
        _ => {
            return Err(Box::new(error(
                StatusCode::NOT_FOUND,
                "not_found",
                "unknown_source",
                None,
            )))
        }
    };
    authenticate_key(key, headers, path, method, body)
}

/// 任一受信来源密钥有效即可（用于运维类通道，如显式评估 tick）。
fn authenticate_any(
    state: &AppState,
    headers: &HeaderMap,
    path: &str,
    method: &str,
    body: &[u8],
) -> Result<(), Box<Response>> {
    let keys: [&[u8]; 3] = [&state.keys.autonomous, &state.keys.remote, &state.keys.estop];
    let sig = headers
        .get("x-signature")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if sig.is_empty() {
        return Err(Box::new(error(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "missing_signature",
            Some("provide X-Signature: hex=<hmac-sha256>".into()),
        )));
    }
    let sim_hdr = headers
        .get("x-sim-at")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if keys
        .iter()
        .any(|k| verify_hex(k, sig, method, path, sim_hdr, body))
    {
        Ok(())
    } else {
        Err(Box::new(error(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "bad_signature",
            Some("HMAC mismatch; sign METHOD\\nPATH\\nX-Sim-At\\nBODY".into()),
        )))
    }
}

fn authenticate_key(
    key: &[u8],
    headers: &HeaderMap,
    path: &str,
    method: &str,
    body: &[u8],
) -> Result<(), Box<Response>> {
    let sig = headers
        .get("x-signature")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if sig.is_empty() {
        return Err(Box::new(error(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "missing_signature",
            Some("provide X-Signature: hex=<hmac-sha256>".into()),
        )));
    }
    let sim_hdr = headers
        .get("x-sim-at")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !verify_hex(key, sig, method, path, sim_hdr, body) {
        return Err(Box::new(error(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "bad_signature",
            Some("HMAC mismatch; sign METHOD\\nPATH\\nX-Sim-At\\nBODY".into()),
        )));
    }
    Ok(())
}

fn ingest_response(
    status: StatusCode,
    kind: &str,
    source: Option<&str>,
    trigger_seq: i64,
    out: crate::arbiter::IngestOutcome,
) -> Response {
    (
        status,
        Json(json!({
            "decision_id": out.decision_id,
            "kind": kind,
            "source": source,
            "trigger_seq": trigger_seq,
            "accepted": out.accepted,
            "effective": out.effective,
            "decision": out.decision,
        })),
    )
        .into_response()
}

// ---------- handlers ----------

async fn health(State(s): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "clock_mode": s.arbiter.clock_mode(),
        "sim_now": s.arbiter.sim_now(),
    }))
}

async fn post_command(
    State(s): State<AppState>,
    Path(source_name): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let source = match Source::parse(&source_name) {
        Some(s) => s,
        None => {
            return error(
                StatusCode::NOT_FOUND,
                "not_found",
                "unknown_source",
                Some("source must be 'autonomous' or 'remote'".into()),
            )
        }
    };
    let path = format!("/v1/sources/{source_name}/commands");
    if let Err(r) = authenticate(&s, &source_name, &headers, &path, "POST", &body) {
        return *r;
    }
    let sim_at = match sim_at_header(&headers) {
        Ok(v) => v,
        Err(r) => return *r,
    };
    let req: CommandRequest = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            return error(
                StatusCode::BAD_REQUEST,
                "bad_request",
                VALIDATION,
                Some(format!("invalid JSON: {e}")),
            )
        }
    };
    let seq = req.seq;
    match s.arbiter.ingest_command(source, req, sim_at, Some(wall_now_ms())) {
        Ok(out) => ingest_response(StatusCode::OK, source.as_str(), Some(source.as_str()), seq, out),
        Err(e) => map_arbiter_err(e),
    }
}

async fn post_release(
    State(s): State<AppState>,
    Path(source_name): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let source = match Source::parse(&source_name) {
        Some(s) => s,
        None => {
            return error(StatusCode::NOT_FOUND, "not_found", "unknown_source", None)
        }
    };
    let path = format!("/v1/sources/{source_name}/leases/release");
    if let Err(r) = authenticate(&s, &source_name, &headers, &path, "POST", &body) {
        return *r;
    }
    let sim_at = match sim_at_header(&headers) {
        Ok(v) => v,
        Err(r) => return *r,
    };
    let req: ReleaseRequest = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            return error(StatusCode::BAD_REQUEST, "bad_request", VALIDATION, Some(format!("invalid JSON: {e}")))
        }
    };
    let seq = req.seq;
    match s.arbiter.release_lease(source, req, sim_at, Some(wall_now_ms())) {
        Ok(out) => ingest_response(StatusCode::OK, "release", Some(source.as_str()), seq, out),
        Err(e) => map_arbiter_err(e),
    }
}

async fn post_estop(
    State(s): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(r) = authenticate(&s, "estop", &headers, "/v1/estop", "POST", &body) {
        return *r;
    }
    let sim_at = match sim_at_header(&headers) {
        Ok(v) => v,
        Err(r) => return *r,
    };
    let req: EstopRequest = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            return error(StatusCode::BAD_REQUEST, "bad_request", VALIDATION, Some(format!("invalid JSON: {e}")))
        }
    };
    let seq = req.seq;
    match s.arbiter.ingest_estop(req, sim_at, Some(wall_now_ms())) {
        Ok(out) => ingest_response(StatusCode::OK, "estop", Some("estop"), seq, out),
        Err(e) => map_arbiter_err(e),
    }
}

#[derive(Deserialize)]
struct EvaluateBody {
    /// 可选：显式指定评估时刻（毫秒）。sim 模式缺省使用 X-Sim-At；都没有则报错。
    at: Option<i64>,
}

async fn post_evaluate(
    State(s): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // tick 用 estop 之外的独立管理密钥会增加负担；这里要求任意来源有效签名即可，
    // 但为简单起见统一使用 estop 密钥（决策 tick 属于受信运维通道）。
    // tick 属于受信运维通道：任一来源密钥签名均可。
    if let Err(r) = authenticate_any(&s, &headers, "/v1/decisions/evaluate", "POST", &body) {
        return *r;
    }
    let hdr_at = match sim_at_header(&headers) {
        Ok(v) => v,
        Err(r) => return *r,
    };
    let body_at = if body.is_empty() {
        None
    } else {
        match serde_json::from_slice::<EvaluateBody>(&body) {
            Ok(b) => b.at,
            Err(e) => {
                return error(StatusCode::BAD_REQUEST, "bad_request", VALIDATION, Some(format!("invalid JSON: {e}")))
            }
        }
    };
    let sim_at = hdr_at.or(body_at);
    match s.arbiter.tick(sim_at, Some(wall_now_ms())) {
        Ok(out) => ingest_response(StatusCode::OK, "evaluate", None, 0, out),
        Err(e) => map_arbiter_err(e),
    }
}

#[derive(Deserialize)]
struct AtQuery {
    at: Option<i64>,
}

/// 只读投影。不写库、不推进 sim_now、不刷新任何租约。
async fn get_decision(State(s): State<AppState>, Query(q): Query<AtQuery>) -> Json<Decision> {
    let at = q.at.or_else(|| match s.arbiter.clock_mode() {
        ClockMode::Wall => Some(wall_now_ms()),
        ClockMode::Sim => None,
    });
    Json(s.arbiter.snapshot(at))
}

#[derive(Deserialize)]
struct LimitQuery {
    limit: Option<i64>,
}

async fn list_decisions(State(s): State<AppState>, Query(q): Query<LimitQuery>) -> Json<serde_json::Value> {
    Json(json!({ "decisions": s.arbiter.list_decisions(q.limit.unwrap_or(50)) }))
}

async fn list_ingest_events(State(s): State<AppState>, Query(q): Query<LimitQuery>) -> Json<serde_json::Value> {
    Json(json!({ "events": s.arbiter.list_ingest_events(q.limit.unwrap_or(50)) }))
}

async fn get_state(State(s): State<AppState>) -> Json<serde_json::Value> {
    let mut v = s.arbiter.debug_state();
    v["now_wall_ms"] = json!(wall_now_ms());
    Json(v)
}

async fn post_admin_reset(
    State(s): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let expected = match &s.keys.admin_token {
        Some(t) => t,
        None => {
            return error(
                StatusCode::FORBIDDEN,
                "forbidden",
                "admin_disabled",
                Some("set ARBITER_ADMIN_TOKEN to enable".into()),
            )
        }
    };
    let provided = headers
        .get("x-admin-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let ok_eq: bool = subtle::ConstantTimeEq::ct_eq(provided.as_bytes(), expected.as_bytes()).into();
    if !ok_eq {
        return error(StatusCode::UNAUTHORIZED, "unauthorized", "bad_admin_token", None);
    }
    let sim_now: i64 = match q.get("sim_now") {
        Some(x) => match x.parse::<i64>() {
            Ok(v) => v,
            Err(_) => {
                return error(
                    StatusCode::BAD_REQUEST,
                    "bad_request",
                    VALIDATION,
                    Some("sim_now must be integer unix-ms".into()),
                )
            }
        },
        None => 1_000_000_000_000, // 2001-09-09T01:46:40Z
    };
    s.arbiter.admin_reset(sim_now);
    (StatusCode::OK, Json(json!({"reset": true, "sim_now": sim_now}))).into_response()
}
