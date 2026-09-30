//! Axum HTTP layer. Exposes typed-batch catalog introspection, three-valued
//! query evaluation, versioned deletion, and on-demand differential
//! verification.
//!
//! Every response uses a consistent envelope (`ok:true` with `data`, or
//! `ok:false` with an `error_code` category) — UNKNOWN/exceptional states are
//! never reported as success. Each request is logged with a generated
//! `run_id` so logs can be correlated with inputs.

pub mod state;

use axum::{
    extract::{Path, Query as AxumQuery, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tracing::{info, warn};

use crate::config::Config;
use crate::error::{ErrorBody, TritmapError};
use crate::query::parse_predicate;

pub use state::AppState;

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/tables", get(list_tables))
        .route("/tables/{name}", get(table_info))
        .route("/tables/{name}/query", post(query_table))
        .route("/tables/{name}/rows/{row}/delete", post(delete_row))
        .route("/verify", post(run_verify))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "ok": true, "data": { "status": "up", "version": env!("CARGO_PKG_VERSION") } }))
}

#[derive(Serialize)]
struct TableMeta {
    name: String,
    rows: usize,
    data_version: u64,
    current_version: u64,
    live_rows: usize,
    deleted_rows: usize,
    indexed_columns: Vec<String>,
    columns: Vec<ColumnMeta>,
}

#[derive(Serialize)]
struct ColumnMeta {
    name: String,
    r#type: &'static str,
}

async fn list_tables(State(st): State<Arc<AppState>>) -> ApiResult<Json<serde_json::Value>> {
    let names = st.table_names();
    Ok(Json(ok_payload(serde_json::json!({ "tables": names }))))
}

async fn table_info(
    State(st): State<Arc<AppState>>,
    Path(name): Path<String>,
) -> ApiResult<Json<serde_json::Value>> {
    let run_id = st.next_run_id();
    let meta = st
        .with_table(&name, |d| {
            Ok(TableMeta {
                name: d.name().to_string(),
                rows: d.row_count(),
                data_version: d.data_version(),
                current_version: d.current_version(),
                live_rows: d.deletions().current_alive().count_ones(),
                deleted_rows: d.deletions().tombstones().len(),
                indexed_columns: d.indexed_columns().into_iter().map(String::from).collect(),
                columns: d
                    .columns()
                    .iter()
                    .map(|c| ColumnMeta {
                        name: c.name.clone(),
                        r#type: c.ty.as_str(),
                    })
                    .collect(),
            })
        })
        .map_err(|e| ApiError::new(run_id.clone(), e))?;
    Ok(Json(ok_payload(serde_json::to_value(meta).unwrap())))
}

#[derive(Deserialize)]
struct QueryBody {
    expr: String,
    /// Pin the query to a specific table version (defaults to current).
    version: Option<u64>,
}

#[derive(Serialize)]
struct QueryResponse {
    version: u64,
    row_space: usize,
    live_rows: usize,
    counts: ResponseCounts,
    selected_rows: Vec<usize>,
    /// Per-row classification for live rows only: TRUE/FALSE/UNKNOWN.
    rows: Vec<RowVerdict>,
}

#[derive(Serialize)]
struct ResponseCounts {
    true_rows: usize,
    false_rows: usize,
    unknown_rows: usize,
    deleted_rows: usize,
}

#[derive(Serialize)]
struct RowVerdict {
    row: usize,
    verdict: String,
}

async fn query_table(
    State(st): State<Arc<AppState>>,
    Path(name): Path<String>,
    Json(body): Json<QueryBody>,
) -> ApiResult<Json<serde_json::Value>> {
    let run_id = st.next_run_id();
    info!(run_id = %run_id, table = %name, expr = %body.expr, version = ?body.version, "http: query received");

    let expr = parse_predicate(&body.expr).map_err(|e| ApiError::new(run_id.clone(), e))?;
    let resp = st
        .with_table(&name, |d| {
            let outcome = crate::query::evaluate(d, &expr, body.version)?;
            let rows = outcome
                .partition
                .to_row_values()
                .into_iter()
                .enumerate()
                .filter_map(|(row, v)| {
                    v.map(|t| RowVerdict {
                        row,
                        verdict: t.as_str().to_string(),
                    })
                })
                .collect();
            Ok(QueryResponse {
                version: outcome.version,
                row_space: outcome.n,
                live_rows: outcome.live,
                counts: ResponseCounts {
                    true_rows: outcome.true_count(),
                    false_rows: outcome.false_count(),
                    unknown_rows: outcome.unknown_count(),
                    deleted_rows: outcome.deleted_count(),
                },
                selected_rows: outcome.selected_rows(),
                rows,
            })
        })
        .map_err(|e| ApiError::new(run_id.clone(), e))?;
    info!(run_id = %run_id, selected = resp.counts.true_rows, unknown = resp.counts.unknown_rows, "http: query complete");
    Ok(Json(ok_payload(serde_json::to_value(resp).unwrap())))
}

#[derive(Deserialize)]
struct DeleteBody {
    version: u64,
}

async fn delete_row(
    State(st): State<Arc<AppState>>,
    Path((name, row)): Path<(String, usize)>,
    Json(body): Json<DeleteBody>,
) -> ApiResult<Json<serde_json::Value>> {
    let run_id = st.next_run_id();
    info!(run_id = %run_id, table = %name, row, version = body.version, "http: delete received");
    st.delete_row(&name, row, body.version)
        .map_err(|e| ApiError::new(run_id.clone(), e))?;
    Ok(Json(ok_payload(serde_json::json!({
        "deleted": row,
        "version": body.version,
    }))))
}

#[derive(Deserialize)]
struct VerifyBody {
    seed: Option<u64>,
    iterations: Option<usize>,
    rows: Option<usize>,
    null_density: Option<u32>,
}

async fn run_verify(
    State(st): State<Arc<AppState>>,
    body: AxumQuery<VerifyBody>,
) -> ApiResult<Json<serde_json::Value>> {
    let cfg: Config = st.config();
    let seed = body.seed.unwrap_or(cfg.verify.seed);
    let iterations = body.iterations.unwrap_or(cfg.verify.iterations);
    let rows = body.rows.unwrap_or(cfg.verify.rows);
    let null_density = body.null_density.unwrap_or(cfg.verify.null_density);
    let reports = st
        .run_differential_verify(seed, iterations, rows, null_density)
        .map_err(|e| ApiError::new(String::from("verify-endpoint"), e))?;
    let all_passed = reports.iter().all(|r| r.passed);
    Ok(Json(ok_payload(serde_json::json!({
        "all_passed": all_passed,
        "cases": reports.len(),
        "reports": reports,
    }))))
}

fn ok_payload(data: serde_json::Value) -> serde_json::Value {
    serde_json::json!({ "ok": true, "data": data })
}

type ApiResult<T> = std::result::Result<T, ApiError>;

/// Local error wrapper carrying the correlating `run_id`. A dedicated newtype
/// is required by the orphan rule (a bare `(String, TritmapError)` tuple is a
/// foreign type and cannot implement `IntoResponse`). The error is boxed to
/// keep `Result` small on the success path.
pub struct ApiError {
    run_id: String,
    error: Box<TritmapError>,
}

impl ApiError {
    pub fn new(run_id: String, error: TritmapError) -> Self {
        Self {
            run_id,
            error: Box::new(error),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let err = *self.error;
        let status = match &err {
            TritmapError::UnknownTable(_) | TritmapError::UnknownColumn(_) => StatusCode::NOT_FOUND,
            TritmapError::RowAlreadyDeleted { .. }
            | TritmapError::VersionOutOfOrder { .. }
            | TritmapError::IncompatibleIndexVersion { .. } => StatusCode::CONFLICT,
            TritmapError::RowNotFound { .. } => StatusCode::NOT_FOUND,
            TritmapError::QueryParse { .. }
            | TritmapError::TypeMismatch { .. }
            | TritmapError::SchemaMismatch(_)
            | TritmapError::UnsupportedType(_)
            | TritmapError::Config(_)
            | TritmapError::Csv(_) => StatusCode::BAD_REQUEST,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        warn!(run_id = %self.run_id, error_code = err.code(), error = %err, "http: request failed");
        let body = ErrorBody::from(&err);
        (status, Json(serde_json::to_value(body).unwrap())).into_response()
    }
}

/// Run the bound TCP listener (used by `main` and integration tests).
pub async fn serve(
    listener: tokio::net::TcpListener,
    state: Arc<AppState>,
) -> Result<(), std::io::Error> {
    let app = router(state);
    info!("http: listening on {}", listener.local_addr()?);
    axum::serve(listener, app).await
}
