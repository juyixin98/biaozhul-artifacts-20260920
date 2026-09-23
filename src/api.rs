//! HTTP API (Axum): snapshot upload/listing and exact quotes.

use std::sync::Arc;

use axum::{
    extract::Path as AxumPath,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::amount::Amount;
use crate::db::Store;
use crate::model::Snapshot;
use crate::router::{best_route, minimum_output, RouteError, RouteResult, SearchStats};
use crate::swap::SwapHop;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

pub fn router(store: Arc<Store>) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/snapshots", get(list_snapshots).post(upload_snapshot))
        .route("/v1/snapshots/:id", get(get_snapshot))
        .route("/v1/snapshots/:id/quote", post(quote))
        .with_state(state)
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

// ---------- errors ----------

#[derive(Debug, Serialize)]
struct ErrorBody {
    error: ApiErrorPayload,
}

#[derive(Debug, Serialize)]
struct ApiErrorPayload {
    code: &'static str,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<serde_json::Value>,
}

enum ApiError {
    BadRequest(&'static str, String),
    InvalidSnapshot(String),
    SnapshotNotFound(String),
    AssetUnknown(String),
    NoRoute {
        paths_considered: usize,
        failures: serde_json::Value,
    },
    Internal(String),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message, details) = match self {
            ApiError::BadRequest(code, m) => (StatusCode::BAD_REQUEST, code, m, None),
            ApiError::InvalidSnapshot(m) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "INVALID_SNAPSHOT",
                m,
                None,
            ),
            ApiError::SnapshotNotFound(id) => (
                StatusCode::NOT_FOUND,
                "SNAPSHOT_NOT_FOUND",
                format!("snapshot {id:?} not found"),
                None,
            ),
            ApiError::AssetUnknown(m) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "ASSET_PRECISION_UNKNOWN",
                m,
                None,
            ),
            ApiError::NoRoute {
                paths_considered,
                failures,
            } => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "NO_ROUTE",
                format!("no feasible route within 3 hops; {paths_considered} candidate paths examined"),
                Some(serde_json::json!({ "paths_considered": paths_considered, "edge_failures": failures })),
            ),
            ApiError::Internal(m) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "INTERNAL",
                m,
                None,
            ),
        };
        let body = ErrorBody {
            error: ApiErrorPayload {
                code,
                message,
                details,
            },
        };
        (status, Json(body)).into_response()
    }
}

fn db_err(e: rusqlite::Error) -> ApiError {
    ApiError::Internal(format!("sqlite: {e}"))
}

// ---------- handlers ----------

#[derive(Debug, Serialize)]
struct UploadResponse {
    snapshot_id: String,
    created: bool,
}

async fn upload_snapshot(
    axum::extract::State(state): axum::extract::State<AppState>,
    Json(snap): Json<Snapshot>,
) -> Result<(StatusCode, Json<UploadResponse>), ApiError> {
    snap.validate().map_err(|e| ApiError::InvalidSnapshot(e.to_string()))?;
    let (id, created) = state.store.put(&snap).map_err(db_err)?;
    Ok((
        if created {
            StatusCode::CREATED
        } else {
            StatusCode::OK
        },
        Json(UploadResponse {
            snapshot_id: id,
            created,
        }),
    ))
}

#[derive(Debug, Serialize)]
struct ListResponse {
    snapshot_ids: Vec<String>,
}

async fn list_snapshots(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<Json<ListResponse>, ApiError> {
    let ids = state.store.list().map_err(db_err)?;
    Ok(Json(ListResponse { snapshot_ids: ids }))
}

async fn get_snapshot(
    axum::extract::State(state): axum::extract::State<AppState>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Snapshot>, ApiError> {
    let stored = state
        .store
        .get(&id)
        .map_err(db_err)?
        .ok_or(ApiError::SnapshotNotFound(id))?;
    Ok(Json(stored.snapshot))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct QuoteRequest {
    asset_in: String,
    asset_out: String,
    amount_in: Amount,
    #[serde(default = "default_slippage")]
    slippage_bps: u32,
}

fn default_slippage() -> u32 {
    0
}

#[derive(Debug, Serialize)]
struct QuoteResponse {
    snapshot_id: String,
    asset_in: String,
    asset_out: String,
    asset_in_decimals: u8,
    asset_out_decimals: u8,
    amount_in: Amount,
    amount_out: Amount,
    slippage_bps: u32,
    min_amount_out: Amount,
    token_path: Vec<String>,
    pool_path: Vec<String>,
    hops: Vec<SwapHop>,
    stats: SearchStats,
}

fn route_response(snapshot_id: String, snap: &Snapshot, req: &QuoteRequest, r: RouteResult) -> QuoteResponse {
    let min_amount_out = minimum_output(r.amount_out, req.slippage_bps)
        .expect("slippage already validated");
    QuoteResponse {
        snapshot_id,
        asset_in_decimals: snap.asset(&req.asset_in).map(|a| a.decimals).unwrap_or(0),
        asset_out_decimals: snap.asset(&req.asset_out).map(|a| a.decimals).unwrap_or(0),
        asset_in: r.asset_in,
        asset_out: r.asset_out,
        amount_in: r.amount_in,
        amount_out: r.amount_out,
        slippage_bps: req.slippage_bps,
        min_amount_out,
        token_path: r.token_path,
        pool_path: r.pool_path,
        hops: r.hops,
        stats: r.stats,
    }
}

async fn quote(
    axum::extract::State(state): axum::extract::State<AppState>,
    AxumPath(id): AxumPath<String>,
    Json(req): Json<QuoteRequest>,
) -> Result<Json<QuoteResponse>, ApiError> {
    if req.slippage_bps > 10_000 {
        return Err(ApiError::BadRequest(
            "BAD_SLIPPAGE",
            format!("slippage_bps={} outside 0..=10000", req.slippage_bps),
        ));
    }
    let stored = state
        .store
        .get(&id)
        .map_err(db_err)?
        .ok_or(ApiError::SnapshotNotFound(id.clone()))?;

    match best_route(&stored.snapshot, &req.asset_in, &req.asset_out, req.amount_in) {
        Ok(r) => Ok(Json(route_response(id, &stored.snapshot, &req, r))),
        Err(RouteError::UnknownAsset(a)) => Err(ApiError::AssetUnknown(format!(
            "asset {a:?} is not present in snapshot {id:?}; its precision is unknown"
        ))),
        Err(RouteError::ZeroInput) => {
            Err(ApiError::BadRequest("ZERO_INPUT", "amount_in must be a positive integer string".into()))
        }
        Err(RouteError::BadSlippage(b)) => Err(ApiError::BadRequest(
            "BAD_SLIPPAGE",
            format!("slippage_bps={b} outside 0..=10000"),
        )),
        Err(RouteError::NoRoute {
            paths_considered,
            failures,
        }) => Err(ApiError::NoRoute {
            paths_considered,
            failures: serde_json::to_value(failures).unwrap_or(serde_json::json!([])),
        }),
    }
}
