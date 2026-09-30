//! Axum HTTP surface: validation/execution endpoints with request identity,
//! structured logging, and explainable responses.
//!
//! Endpoints:
//!   GET  /health           liveness + loaded table names
//!   GET  /version          engine version and key dependency versions
//!   GET  /tables           catalog listing
//!   POST /query            run one query (body: QueryRequest)
//!
//! Every response carries `X-Request-Id`; if the client supplies one it is
//! reused, otherwise a UUIDv4 is generated. The same id is used in the tracing
//! span and echoed in the JSON body, so logs, request, and report line up.

use std::sync::Arc;

use axum::{
    extract::State,
    http::{HeaderMap, HeaderName, StatusCode},
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use tracing::{debug, warn, Instrument};
use uuid::Uuid;

use crate::config::Config;
use crate::service::{JobReport, Mode, QueryJob};
use crate::state::{Catalog, FixtureTable};

#[derive(Clone)]
pub struct AppState {
    pub catalog: Arc<Catalog>,
    pub config: Arc<Config>,
}

pub fn router(catalog: Catalog, config: Config) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/version", get(version))
        .route("/tables", get(tables))
        .route("/query", post(run_query))
        .with_state(AppState {
            catalog: Arc::new(catalog),
            config: Arc::new(config),
        })
}

async fn health(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "engine_version": crate::decorrel::ENGINE_VERSION,
        "tables": s.catalog.table_names(),
    }))
}

async fn version() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "engine": env!("CARGO_PKG_NAME"),
        "engine_version": crate::decorrel::ENGINE_VERSION,
        "rust": "arrow2/axum build",
        "key_dependencies": {
            "axum": "0.7.x",
            "arrow2": "0.18.0",
            "sqlparser": "0.47.0",
        },
        "supported_subquery_forms": ["EXISTS", "NOT EXISTS", "scalar COUNT/SUM", "scalar column"],
        "rejected_forms": ["NOT IN", "IN (subquery)", "OR", "JOIN", "GROUP BY", "nested subqueries"],
    }))
}

async fn tables(State(s): State<AppState>) -> Json<serde_json::Value> {
    let tables: Vec<serde_json::Value> = s
        .catalog
        .table_names()
        .into_iter()
        .map(|name| {
            let t = s.catalog.get(&name).expect("listed table exists");
            serde_json::json!({
                "name": name,
                "row_count": t.batch.n_rows,
                "columns": t.batch.columns.iter().map(|c| serde_json::json!({
                    "name": c.name, "type": c.ty.name()
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    Json(serde_json::json!({ "tables": tables }))
}

#[derive(Debug, Deserialize)]
pub struct QueryRequest {
    /// Optional client-supplied correlation id.
    pub request_id: Option<String>,
    pub sql: String,
    /// interpreter | decorrelated | both (default both).
    pub mode: Option<String>,
    /// Per-request inline tables layered over fixture catalog.
    #[serde(default)]
    pub tables: Vec<FixtureTable>,
}

async fn run_query(
    State(s): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<QueryRequest>,
) -> impl IntoResponse {
    let request_id = req
        .request_id
        .clone()
        .filter(|id| !id.trim().is_empty())
        .or_else(|| {
            headers
                .get("x-request-id")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string())
        })
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    let span = tracing::debug_span!("query", request_id = %request_id);
    let response = async {
        run_job(&s, &request_id, req).await
    }
    .instrument(span)
    .await;

    let status = status_for(&response);
    if status != StatusCode::OK {
        warn!(request_id = %request_id, status = %status, "query failed");
    } else {
        debug!(request_id = %request_id, "query completed");
    }
    (
        status,
        [(
            HeaderName::from_static("x-request-id"),
            request_id
                .parse()
                .unwrap_or_else(|_| axum::http::HeaderValue::from_static("invalid-id")),
        )],
        Json(response),
    )
}

async fn run_job(state: &AppState, request_id: &str, req: QueryRequest) -> JobReport {
    let mode = match req.mode.as_deref() {
        None | Some("") => Mode::Both,
        Some(m) => match Mode::parse(m) {
            Ok(m) => m,
            Err(e) => return bad_request_report(request_id, &req.sql, e),
        },
    };
    // The pipeline is CPU-bound; run it on a blocking thread. The job only
    // borrows `'static` Arcs plus request-owned data, so the move is sound.
    let catalog = Arc::clone(&state.catalog);
    let config = Arc::clone(&state.config);
    let request_id = request_id.to_string();
    let sql = req.sql;
    let inline_tables = req.tables;
    let sql_for_error = sql.clone();
    let request_id_for_error = request_id.clone();
    let joined = tokio::task::spawn_blocking(move || {
        let job = QueryJob {
            request_id: &request_id,
            sql: &sql,
            mode,
            base_catalog: catalog.as_ref(),
            inline_tables,
            config: config.as_ref(),
        };
        job.run()
    })
    .await;
    match joined {
        Ok(report) => report,
        // A panic inside the engine is a bug, not a client error — but the
        // client still deserves a structured, request-correlated failure.
        Err(join_err) => {
            let e = crate::error::EngineError::new(
                crate::error::ErrorKind::Internal,
                format!("query task terminated unexpectedly: {join_err}"),
            );
            bad_request_report(&request_id_for_error, &sql_for_error, e)
        }
    }
}

fn bad_request_report(request_id: &str, sql: &str, e: crate::error::EngineError) -> JobReport {
    JobReport {
        request_id: request_id.to_string(),
        engine_version: crate::decorrel::ENGINE_VERSION.to_string(),
        sql: sql.to_string(),
        mode: "invalid".to_string(),
        stages: vec![],
        result: None,
        error: Some(crate::service::ErrorDto::from(&e)),
        equivalence: None,
        rewrite_proof: vec![],
        executions: vec![],
        caveats: vec![],
    }
}

/// Map structured error kinds to HTTP status codes.
fn status_for(report: &JobReport) -> StatusCode {
    let Some(e) = &report.error else {
        return StatusCode::OK;
    };
    match e.kind.as_str() {
        "parse_error" | "validation_error" | "unsupported_form" | "cardinality_violation" => {
            StatusCode::UNPROCESSABLE_ENTITY
        }
        "limit_exceeded" => StatusCode::PAYLOAD_TOO_LARGE,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}
