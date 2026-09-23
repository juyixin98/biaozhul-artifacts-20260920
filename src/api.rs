//! HTTP API (Axum). Every handler moves the blocking SQLite call onto a
//! blocking thread so the async runtime is never stalled.

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::chain::{self, ReplayHeight};
use crate::db::{state_root_at_locked, utxo_at_locked, Db};
use crate::error::{Error, Result};
use crate::hash::Hash32;
use crate::model::Block;

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Db>,
}

pub fn router(db: Arc<Db>) -> Router {
    let state = AppState { db };
    Router::new()
        .route("/health", get(health))
        .route("/tip", get(tip))
        .route("/blocks", post(submit_block))
        .route("/blocks/:height", get(get_block))
        .route("/blocks/disconnect", post(disconnect))
        .route("/state-root", get(state_root))
        .route("/utxos/:txid/:vout", get(get_utxo))
        .route("/replay/verify", get(replay_verify))
        .with_state(state)
}

/// Run a blocking chain closure on the blocking thread pool, awaiting the
/// result while keeping `?` error conversion intact.
async fn blocking<F, T>(state: &AppState, f: F) -> Result<T>
where
    F: FnOnce(&Db) -> Result<T> + Send + 'static,
    T: Send + 'static,
{
    let db = state.db.clone();
    join(tokio::task::spawn_blocking(move || f(&db)).await)
}

/// Convert a blocking-task join failure into our error type.
fn join<T>(
    r: std::result::Result<Result<T>, tokio::task::JoinError>,
) -> Result<T> {
    r.map_err(|e| Error::Malformed(format!("internal task failure: {e}")))?
}

#[derive(Serialize)]
struct Health {
    status: &'static str,
}

async fn health() -> Json<Health> {
    Json(Health { status: "ok" })
}

#[derive(Serialize)]
struct Tip {
    height: i64,
    hash: String,
    state_root: String,
}

async fn tip(State(s): State<AppState>) -> Result<Json<Tip>> {
    let db = s.db.clone();
    let (height, hash) =
        join(tokio::task::spawn_blocking(move || db.tip()).await)?;
    let root = state_root_blocking(&s, height).await?;
    Ok(Json(Tip {
        height,
        hash: hash.to_hex(),
        state_root: root.to_hex(),
    }))
}

async fn state_root_blocking(s: &AppState, height: i64) -> Result<Hash32> {
    let db = s.db.clone();
    join(tokio::task::spawn_blocking(move || {
        let conn = db.conn.lock().unwrap();
        // Unknown height must 404, even for the current tip.
        if block_exists(&conn, height)? {
            state_root_at_locked(&conn, height)
        } else {
            Err(Error::UnknownHeight(height))
        }
    })
    .await)
}

fn block_exists(conn: &rusqlite::Connection, height: i64) -> Result<bool> {
    let n: i64 = conn.query_row(
        "SELECT COUNT(*) FROM blocks WHERE height = ?1",
        [height],
        |r| r.get(0),
    )?;
    Ok(n > 0)
}

#[derive(Serialize)]
struct BlockAccepted {
    accepted: bool,
    height: i64,
    hash: String,
    state_root: String,
    fees: i64,
    tx_count: usize,
}

async fn submit_block(
    State(s): State<AppState>,
    Json(block): Json<Block>,
) -> Result<(StatusCode, Json<BlockAccepted>)> {
    let outcome = blocking(&s, move |db| chain::connect_block(db, &block)).await?;
    Ok((
        StatusCode::CREATED,
        Json(BlockAccepted {
            accepted: true,
            height: outcome.height,
            hash: outcome.hash.to_hex(),
            state_root: outcome.state_root.to_hex(),
            fees: outcome.fees,
            tx_count: outcome.tx_count,
        }),
    ))
}

#[derive(Deserialize)]
struct DisconnectReq {
    /// Roll back so the tip becomes this height.
    #[serde(rename = "targetHeight")]
    target_height: i64,
}

#[derive(Serialize)]
struct DisconnectResp {
    tip_height: i64,
    tip_hash: String,
    state_root: String,
    disconnected: usize,
}

async fn disconnect(
    State(s): State<AppState>,
    Json(req): Json<DisconnectReq>,
) -> Result<Json<DisconnectResp>> {
    let out = blocking(&s, move |db| chain::disconnect_blocks(db, req.target_height)).await?;
    Ok(Json(DisconnectResp {
        tip_height: out.tip_height,
        tip_hash: out.tip_hash.to_hex(),
        state_root: out.state_root.to_hex(),
        disconnected: out.disconnected,
    }))
}

#[derive(Serialize)]
struct BlockResp {
    height: i64,
    hash: String,
    #[serde(rename = "prevHash")]
    prev_hash: String,
    #[serde(rename = "merkleRoot")]
    merkle_root: String,
    timestamp: i64,
    block: serde_json::Value,
}

async fn get_block(
    State(s): State<AppState>,
    Path(height): Path<i64>,
) -> Result<Json<BlockResp>> {
    let db = s.db.clone();
    let row = tokio::task::spawn_blocking(move || {
        let conn = db.conn.lock().unwrap();
        conn.query_row(
            "SELECT hash, prev_hash, merkle_root, timestamp, block_json
               FROM blocks WHERE height = ?1",
            [height],
            |r| {
                let h: Vec<u8> = r.get(0)?;
                let p: Vec<u8> = r.get(1)?;
                let m: Vec<u8> = r.get(2)?;
                let ts: i64 = r.get(3)?;
                let j: String = r.get(4)?;
                Ok((h, p, m, ts, j))
            },
        )
        .optional_row()
    })
    .await;
    let row = join(row.map(|res| res.map_err(Error::from)))?
        .ok_or(Error::UnknownHeight(height))?;

    Ok(Json(BlockResp {
        height,
        hash: crate::db::to_hash(&row.0).to_hex(),
        prev_hash: crate::db::to_hash(&row.1).to_hex(),
        merkle_root: crate::db::to_hash(&row.2).to_hex(),
        timestamp: row.3,
        block: serde_json::from_str(&row.4)?,
    }))
}

#[derive(Deserialize)]
struct RootQuery {
    /// MANDATORY. History is only answered at an explicit height; there is no
    /// endpoint that silently returns the current UTXO set labelled as history.
    height: Option<i64>,
}

#[derive(Serialize)]
struct RootResp {
    height: i64,
    state_root: String,
}

async fn state_root(
    State(s): State<AppState>,
    Query(q): Query<RootQuery>,
) -> Result<Json<RootResp>> {
    let height = q.height.ok_or_else(|| {
        Error::Malformed("query parameter `height` is required (e.g. /state-root?height=2)"
            .into())
    })?;
    let root = state_root_blocking(&s, height).await?;
    Ok(Json(RootResp {
        height,
        state_root: root.to_hex(),
    }))
}

#[derive(Serialize)]
struct UtxoResp {
    height: i64,
    txid: String,
    vout: u32,
    value: i64,
    address: String,
    created_height: i64,
}

async fn get_utxo(
    State(s): State<AppState>,
    Path((txid_hex, vout)): Path<(String, u32)>,
    Query(q): Query<RootQuery>,
) -> Result<Json<UtxoResp>> {
    let height = q.height.ok_or_else(|| {
        Error::Malformed("query parameter `height` is required".into())
    })?;
    let txid = Hash32::from_hex(&txid_hex)?;
    let db = s.db.clone();
    let looked_up = join(tokio::task::spawn_blocking(move || {
        let conn = db.conn.lock().unwrap();
        if !block_exists(&conn, height)? {
            return Ok::<_, Error>(None);
        }
        Ok(Some(utxo_at_locked(&conn, &txid, vout, height)?))
    })
    .await)?
    .ok_or(Error::UnknownHeight(height))?;
    let entry = looked_up.ok_or(Error::NoOutpoint {
        txid: txid_hex,
        vout,
        height,
    })?;

    Ok(Json(UtxoResp {
        height,
        txid: entry.txid.to_hex(),
        vout: entry.vout,
        value: entry.value,
        address: entry.address,
        created_height: entry.created_height,
    }))
}

#[derive(Deserialize)]
struct ReplayQuery {
    #[serde(default)]
    height: Option<i64>,
}

#[derive(Serialize)]
struct ReplayResp {
    up_to: i64,
    all_match: bool,
    heights: Vec<ReplayHeight>,
}

async fn replay_verify(
    State(s): State<AppState>,
    Query(q): Query<ReplayQuery>,
) -> Result<Json<ReplayResp>> {
    let db0 = s.db.clone();
    let tip = join(tokio::task::spawn_blocking(move || db0.tip()).await)?.0;
    let up_to = q.height.unwrap_or(tip);
    let heights = blocking(&s, move |db| chain::naive_replay(db, up_to)).await?;
    let all_match = heights.iter().all(|h| h.roots_match);
    Ok(Json(ReplayResp {
        up_to,
        all_match,
        heights,
    }))
}

/// Small helper to turn `query_row`'s `QueryReturnedNoRows` into `Option`.
trait OptionalRow<T> {
    fn optional_row(self) -> rusqlite::Result<Option<T>>;
}

impl<T> OptionalRow<T> for rusqlite::Result<T> {
    fn optional_row(self) -> rusqlite::Result<Option<T>> {
        match self {
            Ok(v) => Ok(Some(v)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }
}
