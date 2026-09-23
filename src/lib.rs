//! Library surface: the same modules the binary uses, exposed so HTTP
//! integration tests can build the real Axum router against a real SQLite
//! file without spawning a process.

pub mod amm;
pub mod db;
pub mod model;
pub mod router;
pub mod service;
pub mod uint256;

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response, Result},
    routing::{get, post},
    Json, Router,
};
use serde::Serialize;
use serde_json::json;

pub use db::Store;
pub use model::{QuoteRequest, QuoteResponse, SnapshotInput, SnapshotResponse};
pub use service::QuoteError;

pub type AppState = Arc<Store>;

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

impl ApiError {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "BAD_REQUEST",
            message: msg.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "error": { "code": self.code, "message": self.message }
        }));
        (self.status, body).into_response()
    }
}

impl From<QuoteError> for ApiError {
    fn from(e: QuoteError) -> Self {
        match e {
            QuoteError::BadRequest(m) => ApiError::bad_request(m),
            QuoteError::SnapshotNotFound(id) => ApiError {
                status: StatusCode::NOT_FOUND,
                code: "SNAPSHOT_NOT_FOUND",
                message: format!("snapshot {id} not found"),
            },
            QuoteError::Route(re) => {
                let (status, code) = match &re {
                    router::RouteError::UnknownToken(_)
                    | router::RouteError::SameToken
                    | router::RouteError::ZeroAmount
                    | router::RouteError::BadMaxHops => {
                        (StatusCode::BAD_REQUEST, "BAD_REQUEST")
                    }
                    router::RouteError::NoPath => (StatusCode::NOT_FOUND, "NO_PATH"),
                    router::RouteError::NoFeasiblePath => {
                        (StatusCode::UNPROCESSABLE_ENTITY, "NO_FEASIBLE_PATH")
                    }
                    router::RouteError::Overflow => {
                        (StatusCode::UNPROCESSABLE_ENTITY, "OVERFLOW")
                    }
                };
                ApiError {
                    status,
                    code,
                    message: re.to_string(),
                }
            }
            QuoteError::OutputBelowMinimum { got, required } => ApiError {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "OUTPUT_BELOW_MINIMUM",
                message: format!("net output {got} is below requested minimum {required}"),
            },
            QuoteError::Overflow(m) => ApiError {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "OVERFLOW",
                message: m,
            },
            QuoteError::Storage(m) => ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                code: "STORAGE_ERROR",
                message: m,
            },
        }
    }
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

async fn post_snapshot(
    State(store): State<AppState>,
    payload: Result<Json<SnapshotInput>, axum::extract::rejection::JsonRejection>,
) -> Result<(StatusCode, Json<SnapshotResponse>), ApiError> {
    let Json(input) = payload.map_err(|e| ApiError::bad_request(e.body_text()))?;
    let (assets, pools) = model::validate_snapshot_input(&input).map_err(ApiError::bad_request)?;
    let hash = Store::content_hash(&assets, &pools);
    let created_at = db::now_rfc3339();
    let id = store
        .insert_snapshot(&assets, &pools, &created_at, &hash)
        .await
        .map_err(|e| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "STORAGE_ERROR",
            message: e.to_string(),
        })?;
    Ok((
        StatusCode::CREATED,
        Json(SnapshotResponse {
            snapshot_id: id,
            created_at,
            content_hash: hash,
            asset_count: assets.len(),
            pool_count: pools.len(),
        }),
    ))
}

#[derive(Serialize)]
pub struct SnapshotMeta {
    pub snapshot_id: i64,
    pub created_at: String,
    pub content_hash: String,
    pub asset_count: usize,
    pub pool_count: usize,
}

async fn get_snapshot(
    State(store): State<AppState>,
    Path(id): Path<i64>,
) -> Result<Json<SnapshotMeta>, ApiError> {
    let snap = store
        .load_snapshot(Some(id))
        .await
        .map_err(|e| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "STORAGE_ERROR",
            message: e.to_string(),
        })?
        .ok_or(ApiError {
            status: StatusCode::NOT_FOUND,
            code: "SNAPSHOT_NOT_FOUND",
            message: format!("snapshot {id} not found"),
        })?;
    Ok(Json(SnapshotMeta {
        snapshot_id: snap.id,
        created_at: snap.created_at,
        content_hash: snap.content_hash,
        asset_count: snap.assets.len(),
        pool_count: snap.pools.len(),
    }))
}

async fn latest_snapshot(
    State(store): State<AppState>,
) -> Result<Json<SnapshotMeta>, ApiError> {
    let snap = store
        .load_snapshot(None)
        .await
        .map_err(|e| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "STORAGE_ERROR",
            message: e.to_string(),
        })?
        .ok_or(ApiError {
            status: StatusCode::NOT_FOUND,
            code: "SNAPSHOT_NOT_FOUND",
            message: "no snapshots exist".to_string(),
        })?;
    Ok(Json(SnapshotMeta {
        snapshot_id: snap.id,
        created_at: snap.created_at,
        content_hash: snap.content_hash,
        asset_count: snap.assets.len(),
        pool_count: snap.pools.len(),
    }))
}

async fn post_quote(
    State(store): State<AppState>,
    payload: Result<Json<QuoteRequest>, axum::extract::rejection::JsonRejection>,
) -> Result<Json<QuoteResponse>, ApiError> {
    let Json(req) = payload.map_err(|e| ApiError::bad_request(e.body_text()))?;
    let resp = service::quote(&store, &req).await?;
    Ok(Json(resp))
}

pub fn app(store: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/snapshots", post(post_snapshot))
        .route("/snapshots/latest", get(latest_snapshot))
        .route("/snapshots/{id}", get(get_snapshot))
        .route("/quotes", post(post_quote))
        .with_state(store)
}
