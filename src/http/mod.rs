//! Axum validation entry: typed JSON contracts over the join engines.
//!
//! Endpoints:
//!
//! * `GET  /health`                         — liveness probe.
//! * `POST /v1/runs`                        — create a run with both inputs.
//! * `POST /v1/runs/:id/append`             — append a batch to one side.
//! * `POST /v1/runs/:id/join`               — next controlled page.
//! * `GET  /v1/runs/:id`                    — run status.
//! * `GET  /v1/runs/:id/explain`            — permutations/maps/groups/metrics.
//! * `POST /v1/join`                        — stateless one-shot join.
//!
//! All errors serialize the unified [`JoinError`] contract with HTTP status
//! derived from the error category.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::batch::{Column, DType, TypedBatch};
use crate::error::JoinError;
use crate::operator::iejoin::{explain, JoinExplain};
use crate::operator::plan::{JoinPlan, JoinPredicate};
use crate::resource::{
    result_size, Engine, PageOutcome, PageRequest, ResourceBudget,
};
use crate::state::{RunRegistry, Side};

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub registry: Arc<RunRegistry>,
    pub budget: ResourceBudget,
}

impl AppState {
    #[must_use]
    pub fn new(budget: ResourceBudget) -> Self {
        AppState { registry: Arc::new(RunRegistry::new()), budget }
    }
}

/// Build the router (also used by in-process HTTP tests).
#[must_use]
pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/runs", post(create_run))
        .route("/v1/runs/:id", get(run_status))
        .route("/v1/runs/:id/append", post(append_batch))
        .route("/v1/runs/:id/join", post(join_page))
        .route("/v1/runs/:id/explain", get(run_explain))
        .route("/v1/join", post(one_shot))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

// ---------------------------------------------------------------------------
// JSON DTOs
// ---------------------------------------------------------------------------

/// One column on the wire. `dtype` selects the values representation.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ColumnDto {
    pub name: String,
    pub dtype: DType,
    /// int64/float64: JSON numbers; null entries are SQL NULL. utf8: strings.
    #[serde(default)]
    pub values: Vec<serde_json::Value>,
}

impl ColumnDto {
    fn into_column_named(self, name: &str) -> Result<Column, JoinError> {
        let col = match self.dtype {
            DType::Int64 => {
                let mut vals = Vec::with_capacity(self.values.len());
                for v in self.values {
                    vals.push(match v {
                        serde_json::Value::Null => None::<i64>,
                        serde_json::Value::Number(n) => Some(
                            n.as_i64()
                                .ok_or_else(|| int_parse_error(name, &n))?,
                        ),
                        other => return Err(type_json_error(name, "integer or null", &other)),
                    });
                }
                Column::from_i64(vals)
            }
            DType::Float64 => {
                let mut vals = Vec::with_capacity(self.values.len());
                for v in self.values {
                    vals.push(match v {
                        serde_json::Value::Null => None,
                        serde_json::Value::Number(n) => Some(
                            n.as_f64()
                                .ok_or_else(|| type_json_error(name, "finite number", &serde_json::Value::Number(n.clone())))?,
                        ),
                        other => return Err(type_json_error(name, "number or null", &other)),
                    });
                }
                Column::from_f64(vals)
            }
            DType::Utf8 => {
                let mut vals = Vec::with_capacity(self.values.len());
                for v in self.values {
                    vals.push(match v {
                        serde_json::Value::Null => None,
                        serde_json::Value::String(s) => Some(s),
                        other => return Err(type_json_error(name, "string or null", &other)),
                    });
                }
                Column::from_utf8(vals)
            }
        };
        Ok(col)
    }
}

fn int_parse_error(name: &str, n: &serde_json::Number) -> JoinError {
    JoinError::input(
        "value_not_int64",
        format!("column {name:?}: {n} cannot be represented as int64"),
    )
    .with("column", name)
}

fn type_json_error(name: &str, expected: &str, got: &serde_json::Value) -> JoinError {
    JoinError::input(
        "value_wrong_json_type",
        format!("column {name:?}: expected {expected}, got {got}"),
    )
    .with("column", name)
}

fn parse_batch(columns: Vec<ColumnDto>) -> Result<TypedBatch, JoinError> {
    let mut built = Vec::with_capacity(columns.len());
    for c in columns {
        let name = c.name.clone();
        built.push((name.clone(), c.into_column_named(&name)?));
    }
    TypedBatch::new(built)
}

#[derive(Debug, Deserialize)]
struct CreateRunRequest {
    #[serde(default = "default_run_prefix")]
    run_id_prefix: String,
    predicates: [JoinPredicate; 2],
    left: Vec<ColumnDto>,
    right: Vec<ColumnDto>,
}

fn default_run_prefix() -> String {
    "run".to_owned()
}

#[derive(Debug, Deserialize)]
struct AppendRequest {
    side: Side,
    columns: Vec<ColumnDto>,
}

#[derive(Debug, Clone, Deserialize)]
struct JoinRequest {
    #[serde(default)]
    engine: Option<Engine>,
    #[serde(default)]
    offset: Option<u64>,
    limit: Option<usize>,
}

impl JoinRequest {
    fn page(&self, budget: &ResourceBudget) -> Result<(Engine, PageRequest), JoinError> {
        let engine = self.engine.unwrap_or(Engine::Iejoin);
        let limit = budget.resolve_limit(self.limit)?;
        Ok((engine, PageRequest { offset: self.offset.unwrap_or(0), limit }))
    }
}

#[derive(Debug, Serialize)]
struct JoinResponse {
    run_id: Option<String>,
    pairs: Vec<[u32; 2]>,
    next_offset: u64,
    complete: bool,
    reason: String,
    result_size: u64,
    metrics: serde_json::Value,
}

fn page_to_response(run_id: Option<String>, page: PageOutcome, total: u64) -> JoinResponse {
    JoinResponse {
        run_id,
        pairs: page.pairs.into_iter().map(|(l, r)| [l, r]).collect(),
        next_offset: page.next_offset,
        complete: page.complete,
        reason: serde_json::to_value(page.reason).unwrap().as_str().unwrap().to_owned(),
        result_size: total,
        metrics: serde_json::to_value(page.metrics).unwrap(),
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn create_run(
    State(state): State<AppState>,
    Json(req): Json<CreateRunRequest>,
) -> Result<Json<serde_json::Value>, JoinError> {
    let left = parse_batch(req.left)?;
    let right = parse_batch(req.right)?;
    state.budget.check_rows(&left, &right)?;
    let id = state.registry.create(&req.run_id_prefix, req.predicates, left, right)?;
    Ok(Json(json!({ "run_id": id })))
}

async fn append_batch(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<AppendRequest>,
) -> Result<Json<serde_json::Value>, JoinError> {
    let batch = parse_batch(req.columns)?;
    state.registry.append(&id, req.side, batch)?;
    let status = state.registry.status(&id)?;
    Ok(Json(json!({ "status": "appended", "run": status })))
}

async fn join_page(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<JoinRequest>,
) -> Result<Json<JoinResponse>, JoinError> {
    let (engine, page) = req.page(&state.budget)?;
    let outcome = state.registry.page(&id, engine, &state.budget, page)?;
    let total = state
        .registry
        .with_explain(&id, |plan, l, r| result_size(engine, plan, l, r, &state.budget))??;
    Ok(Json(page_to_response(Some(id), outcome, total)))
}

async fn run_status(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, JoinError> {
    Ok(Json(json!(state.registry.status(&id)?)))
}

async fn run_explain(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<JoinExplain>, JoinError> {
    let snapshot =
        state.registry.with_explain(&id, |plan, l, r| explain(&id, plan, l, r))??;
    Ok(Json(snapshot))
}

#[derive(Debug, Deserialize)]
struct OneShotRequest {
    predicates: [JoinPredicate; 2],
    left: Vec<ColumnDto>,
    right: Vec<ColumnDto>,
    #[serde(default)]
    engine: Option<Engine>,
    limit: Option<usize>,
}

async fn one_shot(
    State(state): State<AppState>,
    Json(req): Json<OneShotRequest>,
) -> Result<Json<JoinResponse>, JoinError> {
    let left = parse_batch(req.left)?;
    let right = parse_batch(req.right)?;
    let plan = JoinPlan::validate(req.predicates, &left, &right)?;
    let engine = req.engine.unwrap_or(Engine::Iejoin);
    let limit = state.budget.resolve_limit(req.limit)?;
    let page = crate::resource::execute_page(
        engine,
        &plan,
        &left,
        &right,
        &state.budget,
        PageRequest { offset: 0, limit },
    )?;
    let total = result_size(engine, &plan, &left, &right, &state.budget)?;
    Ok(Json(page_to_response(None, page, total)))
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

impl IntoResponse for JoinError {
    fn into_response(self) -> Response {
        let status =
            StatusCode::from_u16(self.category.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let body = Json(json!({
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }));
        (status, body).into_response()
    }
}
