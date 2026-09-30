//! Axum HTTP validation entry.
//!
//! The HTTP layer is intentionally thin: it parses/validates requests and
//! forwards them to the [`crate::engine::Engine`]. Domain failures are
//! serialized with their stable `kind` (mirroring [`crate::error::ErrorKind`])
//! and an appropriate HTTP status — an unknown column, bad predicate or
//! out-of-order delete is a typed client error, never a 200 success.

use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::batch::BatchSchema;
use crate::config::Config;
use crate::engine::{Engine, QueryOutcome};
use crate::error::{ErrorKind, IndexError, Result};
use crate::eval::Strategy;
use crate::predicate::Predicate;
use crate::scalar::Scalar;

type Shared = Arc<Mutex<Engine>>;

/// Build the application router (also used directly by the HTTP test suite).
pub fn router(engine: Shared) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/tables", get(list_tables).post(create_table))
        .route(
            "/v1/tables/:id",
            get(get_table),
        )
        .route("/v1/tables/:id/batches", post(append_batch))
        .route("/v1/tables/:id/deletes", post(delete_rows))
        .route("/v1/tables/:id/indexes", post(create_index))
        .route("/v1/tables/:id/query", post(query_table))
        .with_state(engine)
}

/// Bind and serve until the listener ends.
pub async fn serve(config: &Config, engine: Engine) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind((config.host.as_str(), config.port)).await?;
    tracing::info!(
        host = %config.host,
        port = listener.local_addr()?.port(),
        crate_version = env!("CARGO_PKG_VERSION"),
        "HTTP validation entry listening"
    );
    axum::serve(listener, router(Arc::new(Mutex::new(engine)))).await
}

impl IntoResponse for IndexError {
    fn into_response(self) -> Response {
        let status = StatusCode::from_u16(self.kind.http_status())
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let body = Json(json!({
            "error": {"kind": self.kind.as_str(), "message": self.context()}
        }));
        (status, body).into_response()
    }
}

fn parse_json(body: &Bytes) -> Result<Value> {
    serde_json::from_slice(body).map_err(|e| {
        IndexError::new(
            ErrorKind::MalformedRequest,
            format!("request body is not valid JSON: {e}"),
        )
    })
}

fn field<'a>(value: &'a Value, name: &str) -> Result<&'a Value> {
    value.get(name).ok_or_else(|| {
        IndexError::new(
            ErrorKind::MalformedRequest,
            format!("request object is missing required field {name:?}"),
        )
    })
}

fn parse_cell(value: &Value) -> Result<Option<Scalar>> {
    if value.is_null() {
        return Ok(None);
    }
    let scalar = serde_json::from_value::<Scalar>(value.clone()).map_err(|_| {
        IndexError::new(
            ErrorKind::MalformedRequest,
            format!("{value} is not a supported typed cell (int/float/bool/text or null)"),
        )
    })?;
    Ok(Some(scalar))
}

async fn health(State(engine): State<Shared>) -> impl IntoResponse {
    let e = engine.lock().expect("engine mutex not poisoned");
    Json(json!({
        "status": "ok",
        "crate": env!("CARGO_PKG_NAME"),
        "version": env!("CARGO_PKG_VERSION"),
        "tables": e.list_tables().len(),
    }))
}

#[derive(Deserialize)]
struct CreateTableBody {
    id: String,
    schema: BatchSchema,
}

async fn create_table(
    State(engine): State<Shared>,
    body: Bytes,
) -> Result<Json<Value>> {
    let value = parse_json(&body)?;
    let req: CreateTableBody = serde_json::from_value(value).map_err(|e| {
        IndexError::new(ErrorKind::MalformedRequest, format!("invalid create-table body: {e}"))
    })?;
    if req.id.trim().is_empty() {
        return Err(IndexError::new(ErrorKind::MalformedRequest, "table id must not be empty"));
    }
    {
        let mut e = engine.lock().expect("engine mutex not poisoned");
        e.create_table(&req.id, req.schema)?;
    }
    Ok(Json(json!({"id": req.id, "created": true})))
}

async fn list_tables(State(engine): State<Shared>) -> impl IntoResponse {
    let e = engine.lock().expect("engine mutex not poisoned");
    let tables: Vec<Value> = e
        .list_tables()
        .into_iter()
        .map(|(id, stats)| serde_json::to_value((&id, &stats)).unwrap_or(json!({"id": id})))
        .collect();
    Json(json!({"tables": tables}))
}

async fn get_table(
    State(engine): State<Shared>,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    let e = engine.lock().expect("engine mutex not poisoned");
    let stats = e.table_stats(&id)?;
    Ok(Json(json!({"id": id, "stats": serde_json::to_value(stats).unwrap()})))
}

async fn append_batch(
    State(engine): State<Shared>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>> {
    let value = parse_json(&body)?;
    let raw_columns = field(&value, "columns")?
        .as_array()
        .ok_or_else(|| IndexError::new(ErrorKind::MalformedRequest, "\"columns\" must be an array of column vectors"))?;
    let mut columns = Vec::with_capacity(raw_columns.len());
    for raw_col in raw_columns {
        let cells_raw = raw_col.as_array().ok_or_else(|| {
            IndexError::new(ErrorKind::MalformedRequest, "each column must be an array of cells")
        })?;
        let cells: Vec<Option<Scalar>> = cells_raw.iter().map(parse_cell).collect::<Result<_>>()?;
        columns.push(cells);
    }
    let appended;
    let row_count;
    {
        let mut e = engine.lock().expect("engine mutex not poisoned");
        appended = e.append_columns(&id, columns)?;
        row_count = e.table_stats(&id)?.row_count;
    }
    Ok(Json(json!({"table": id, "appended_rows": appended, "row_count": row_count})))
}

#[derive(Deserialize)]
struct DeleteBody {
    version: u64,
    rows: Vec<usize>,
}

async fn delete_rows(
    State(engine): State<Shared>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>> {
    let value = parse_json(&body)?;
    let req: DeleteBody = serde_json::from_value(value).map_err(|e| {
        IndexError::new(ErrorKind::MalformedRequest, format!("invalid delete body: {e}"))
    })?;
    {
        let mut e = engine.lock().expect("engine mutex not poisoned");
        e.delete_rows(&id, req.version, &req.rows)?;
    }
    let e = engine.lock().expect("engine mutex not poisoned");
    let stats = e.table_stats(&id)?;
    Ok(Json(json!({
        "table": id,
        "version": req.version,
        "deleted_rows": req.rows.len(),
        "current_version": stats.current_version,
        "live_rows": stats.live_rows,
    })))
}

#[derive(Deserialize)]
struct IndexBody {
    columns: Vec<String>,
}

async fn create_index(
    State(engine): State<Shared>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>> {
    let value = parse_json(&body)?;
    let req: IndexBody = serde_json::from_value(value).map_err(|e| {
        IndexError::new(ErrorKind::MalformedRequest, format!("invalid index body: {e}"))
    })?;
    {
        let mut e = engine.lock().expect("engine mutex not poisoned");
        e.register_composite(&id, &req.columns)?;
    }
    Ok(Json(json!({"table": id, "index": req.columns})))
}

async fn query_table(
    State(engine): State<Shared>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<QueryOutcome>> {
    let value = parse_json(&body)?;
    let pred_value = field(&value, "predicate")?;
    let pred = Predicate::from_json(pred_value)?;
    // Strict optional fields: a present-but-malformed "strategy"/"version" is a
    // typed client error, never silently treated as absent.
    let strategy = match value.get("strategy") {
        None | Some(Value::Null) => Strategy::Auto,
        Some(Value::String(raw)) => Strategy::parse(raw)?,
        Some(other) => {
            return Err(IndexError::new(
                ErrorKind::MalformedRequest,
                format!("\"strategy\" must be a string (single|combined|auto), got {other}"),
            ))
        }
    };
    let version = match value.get("version") {
        None | Some(Value::Null) => None,
        Some(v) => Some(v.as_u64().ok_or_else(|| {
            IndexError::new(
                ErrorKind::MalformedRequest,
                format!("\"version\" must be a non-negative integer, got {v}"),
            )
        })?),
    };
    let e = engine.lock().expect("engine mutex not poisoned");
    let outcome = e.query(&id, &pred, strategy, version)?;
    Ok(Json(outcome))
}
