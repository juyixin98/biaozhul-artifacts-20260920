//! HTTP API (JSON; binary keys/values are hex-encoded).
//!
//! | method | path                  | purpose                                  |
//! |--------|-----------------------|------------------------------------------|
//! | GET    | `/healthz`            | liveness                                 |
//! | GET    | `/v1/root`            | current root + metadata                  |
//! | GET    | `/v1/root/{version}`  | historical root (immutable)              |
//! | GET    | `/v1/roots`           | all published versions                   |
//! | POST   | `/v1/batches`         | atomic write batch → new immutable root  |
//! | POST   | `/v1/proofs`          | existence / non-existence proof          |
//! | GET    | `/v1/nodes/{hashhex}` | node preimage for audit                  |

use crate::error::{AppError, AppResult};
use crate::proof;
use crate::store::{Op, Store};
use axum::extract::{Path, Query, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

pub fn router(store: Arc<Store>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/root", get(current_root))
        .route("/v1/roots", get(list_roots))
        .route("/v1/root/{version}", get(root_at))
        .route("/v1/batches", post(apply_batch))
        .route("/v1/proofs", post(get_proof))
        .route("/v1/nodes/{hash}", post(get_node_alt).get(get_node))
        .with_state(AppState { store })
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

fn root_json(r: &crate::store::RootInfo, current: u64) -> serde_json::Value {
    json!({
        "version": r.version,
        "root_hex": hex::encode(r.root),
        "top_note": "root_hex is the version-commit hash binding (version, tree-top, leaf_count, height)",
        "leaf_count": r.leaf_count,
        "height": r.height,
        "created_at_ms": r.created_at_ms,
        "current_version": current,
    })
}

async fn current_root(State(s): State<AppState>) -> AppResult<Json<serde_json::Value>> {
    let info = s.store.root_info(None)?;
    Ok(Json(root_json(&info, info.version)))
}

async fn root_at(
    State(s): State<AppState>,
    Path(version): Path<u64>,
) -> AppResult<Json<serde_json::Value>> {
    let info = s.store.root_info(Some(version))?;
    Ok(Json(root_json(&info, s.store.current_version())))
}

#[derive(Deserialize)]
struct RootsQuery {
    limit: Option<u64>,
}

async fn list_roots(
    State(s): State<AppState>,
    Query(q): Query<RootsQuery>,
) -> AppResult<Json<serde_json::Value>> {
    let all = s.store.list_roots()?;
    let cur = s.store.current_version();
    let limit = q.limit.unwrap_or(all.len() as u64) as usize;
    let items: Vec<serde_json::Value> = all.iter().take(limit).map(|r| root_json(r, cur)).collect();
    Ok(Json(json!({ "versions": items, "current_version": cur })))
}

#[derive(Deserialize)]
struct WriteOp {
    key_hex: Option<String>,
    value_hex: Option<String>,
    /// Convenience for readable examples: UTF-8 used only if the *_hex field
    /// is absent.
    key: Option<String>,
    value: Option<String>,
    /// When true, delete the key (value_hex/value ignored).
    #[serde(default)]
    delete: bool,
}

#[derive(Deserialize)]
struct BatchReq {
    writes: Vec<WriteOp>,
}

#[derive(Serialize)]
struct BatchResp {
    version: u64,
    root_hex: String,
    leaf_count: u64,
    height: u32,
    applied_puts: usize,
    applied_deletes: usize,
}

fn decode_field(hex_field: Option<&str>, raw: Option<&str>, what: &str) -> AppResult<Vec<u8>> {
    if let Some(hx) = hex_field {
        hex::decode(hx)
            .map_err(|e| AppError::bad_request(format!("{what}_hex is not valid hex: {e}")))
    } else if let Some(s) = raw {
        Ok(s.as_bytes().to_vec())
    } else {
        Err(AppError::bad_request(format!(
            "each write needs {what}_hex or {what}"
        )))
    }
}

/// Value decoding: explicit `value_hex` wins; `value` is UTF-8; if neither is
/// present on a put, the value is the *empty byte string* (present-empty,
/// distinct from delete).
fn decode_value(op: &WriteOp) -> AppResult<Vec<u8>> {
    if let Some(hx) = &op.value_hex {
        hex::decode(hx).map_err(|e| AppError::bad_request(format!("value_hex invalid hex: {e}")))
    } else if let Some(s) = &op.value {
        Ok(s.as_bytes().to_vec())
    } else {
        Ok(Vec::new())
    }
}

async fn apply_batch(
    State(s): State<AppState>,
    Json(req): Json<BatchReq>,
) -> AppResult<Json<BatchResp>> {
    let mut writes = Vec::with_capacity(req.writes.len());
    for (i, op) in req.writes.iter().enumerate() {
        let key = decode_field(op.key_hex.as_deref(), op.key.as_deref(), "key")
            .map_err(|e| tag_index(e, i))?;
        let op = if op.delete {
            Op::Delete
        } else {
            Op::Put(decode_value(op).map_err(|e| tag_index(e, i))?)
        };
        writes.push((key, op));
    }
    let out = s.store.commit(writes)?;
    Ok(Json(BatchResp {
        version: out.version,
        root_hex: hex::encode(out.root),
        leaf_count: out.leaf_count,
        height: out.height,
        applied_puts: out.applied_puts,
        applied_deletes: out.applied_deletes,
    }))
}

fn tag_index(e: AppError, i: usize) -> AppError {
    match e {
        AppError::BadRequest(m) => AppError::bad_request(format!("writes[{i}]: {m}")),
        other => other,
    }
}

#[derive(Deserialize)]
struct ProofReq {
    key_hex: Option<String>,
    key: Option<String>,
    version: Option<u64>,
}

async fn get_proof(
    State(s): State<AppState>,
    Json(req): Json<ProofReq>,
) -> AppResult<Json<serde_json::Value>> {
    let key = decode_field(req.key_hex.as_deref(), req.key.as_deref(), "key")?;
    let doc = proof::generate(&s.store, req.version, &key)?;
    Ok(Json(doc))
}

async fn get_node(
    State(s): State<AppState>,
    Path(hashhex): Path<String>,
) -> AppResult<Json<serde_json::Value>> {
    node_payload(&s, &hashhex)
}

// Allowed POST too so curl -d works; same payload.
async fn get_node_alt(
    State(s): State<AppState>,
    Path(hashhex): Path<String>,
) -> AppResult<Json<serde_json::Value>> {
    node_payload(&s, &hashhex)
}

fn node_payload(s: &AppState, hashhex: &str) -> AppResult<Json<serde_json::Value>> {
    let hash = hex::decode(hashhex)
        .map_err(|e| AppError::bad_request(format!("hash not valid hex: {e}")))?;
    let hash: [u8; 32] = hash
        .try_into()
        .map_err(|_| AppError::bad_request("node hash must be 32 bytes"))?;
    match s.store.node_preimage(&hash)? {
        Some(bytes) => Ok(Json(json!({
            "hash_hex": hashhex,
            "tag": match bytes.first() {
                Some(0) => "leaf",
                Some(1) => "inner",
                _ => "unknown",
            },
            "preimage_hex": hex::encode(bytes),
        }))),
        None => Err(AppError::not_found("no node record for that hash")),
    }
}
