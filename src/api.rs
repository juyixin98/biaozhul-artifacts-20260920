//! Axum HTTP API.
//!
//! Endpoints:
//! ```text
//! GET  /health
//! GET  /chain/tip
//! GET  /blocks/tip
//! GET  /blocks/{height}            (also ?by_hash=...)
//! POST /blocks                     submit / connect a block (409 on reject)
//! POST /blocks/disconnect          roll back the tip
//! GET  /utxos                      CURRENT live UTXO set (?address=)
//! GET  /utxos/at/{height}         historical UTXO set, explicit height (?address=)
//! GET  /debug/replay               naive-replay cross-check of the full chain
//! ```

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;

use crate::error::{AppError, AppResult};
use crate::naive;
use crate::storage::Storage;
use crate::types::Block;

#[derive(Clone)]
pub struct AppState {
    pub storage: Arc<Storage>,
}

pub fn router(storage: Arc<Storage>) -> Router {
    let state = AppState { storage };
    Router::new()
        .route("/health", get(health))
        .route("/chain/tip", get(chain_tip))
        .route("/blocks/tip", get(block_tip))
        .route("/blocks/{height}", get(get_block))
        .route("/blocks", post(submit_block))
        .route("/blocks/disconnect", post(disconnect))
        .route("/utxos", get(current_utxos))
        .route("/utxos/at/{height}", get(history_utxos))
        .route("/debug/replay", get(debug_replay))
        .with_state(state)
}

async fn health() -> impl IntoResponse {
    Json(json!({ "status": "ok", "service": "utxo-rollback-validator" }))
}

async fn chain_tip(State(s): State<AppState>) -> AppResult<impl IntoResponse> {
    match s.storage.tip()? {
        Some(tip) => Ok(Json(json!({ "empty": false, "tip": tip }))),
        None => Ok(Json(json!({ "empty": true }))),
    }
}

async fn block_tip(State(s): State<AppState>) -> AppResult<impl IntoResponse> {
    match s.storage.tip()? {
        Some(tip) => match s.storage.active_block_at_height(tip.height)? {
            Some(info) => Ok(Json(info)),
            None => Err(AppError::internal("tip height has no block row")),
        },
        None => Err(AppError::not_found("chain is empty")),
    }
}

#[derive(Deserialize)]
struct BlockQuery {
    #[serde(default)]
    by_hash: Option<String>,
}

async fn get_block(
    State(s): State<AppState>,
    Path(height_str): Path<String>,
    Query(q): Query<BlockQuery>,
) -> AppResult<Response> {
    if let Some(hash) = q.by_hash {
        let info = s
            .storage
            .block_by_hash(&hash)?
            .ok_or_else(|| AppError::not_found(format!("unknown block hash {hash}")))?;
        return Ok((StatusCode::OK, Json(info)).into_response());
    }
    let height: u64 = height_str
        .parse()
        .map_err(|_| AppError::bad_request("height must be a non-negative integer"))?;
    let info = s
        .storage
        .active_block_at_height(height)?
        .ok_or_else(|| AppError::not_found(format!("no active-chain block at height {height}")))?;
    Ok((StatusCode::OK, Json(info)).into_response())
}

/// Submit a block. Malformed JSON / negative amounts are 400; any protocol
/// rejection is 409 with the precise reason and the database is untouched.
async fn submit_block(
    State(s): State<AppState>,
    body: Bytes,
) -> AppResult<(StatusCode, Json<serde_json::Value>)> {
    let block: Block = serde_json::from_slice(&body).map_err(|e| {
        AppError::bad_request(format!("invalid block JSON: {e}"))
    })?;
    let result = s.storage.connect_block(block)?;
    Ok((StatusCode::CREATED, Json(json!(result))))
}

async fn disconnect(State(s): State<AppState>) -> AppResult<Json<serde_json::Value>> {
    let result = s.storage.disconnect_tip()?;
    Ok(Json(json!(result)))
}

#[derive(Deserialize)]
struct UtxoQuery {
    #[serde(default)]
    address: Option<String>,
}

async fn current_utxos(
    State(s): State<AppState>,
    Query(q): Query<UtxoQuery>,
) -> AppResult<Json<serde_json::Value>> {
    let (root, utxos) = s.storage.current_utxos(q.address.as_deref())?;
    Ok(Json(json!({
        "view": "current",
        "state_root": root,
        "count": utxos.len(),
        "utxos": utxos,
    })))
}

async fn history_utxos(
    State(s): State<AppState>,
    Path(height_str): Path<String>,
    Query(q): Query<UtxoQuery>,
) -> AppResult<Json<serde_json::Value>> {
    let height: u64 = height_str
        .parse()
        .map_err(|_| AppError::bad_request("height must be a non-negative integer"))?;
    let (root, utxos) = s.storage.utxo_at_height(height, q.address.as_deref())?;
    Ok(Json(json!({
        "view": "historical",
        "height": height,
        "state_root": root,
        "count": utxos.len(),
        "utxos": utxos,
    })))
}

async fn debug_replay(State(s): State<AppState>) -> AppResult<Json<naive::ReplayReport>> {
    // Replay needs a raw connection; Storage exposes the higher-level ops, so
    // we open the same database file read-only through the storage's own lock
    // by adding a small passthrough on Storage.
    let report = s.storage.run_naive_replay()?;
    Ok(Json(report))
}
