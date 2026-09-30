//! Axum HTTP surface: ingestion, query, resume and verification entry points.
//!
//! Every handler runs under the request-id middleware. Accepted, incomplete
//! and rejected requests all produce a structured diagnostic event carrying
//! the request id, catalog version and key execution state (variable count,
//! index policy, access counters, row counts). Row *values* are never logged;
//! typed scalars expose a redacted rendering for error paths.

pub mod diagnostics;

use axum::body::{to_bytes, Body};
use axum::extract::{Path, Query as QueryExtractor, State};
use axum::http::{header, HeaderMap, HeaderValue, Request, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tracing::{info, warn};

use crate::error::{Error, ReasonCode};
use crate::fixtures;
use crate::leapfrog::{self, Cursor, RunParams};
use crate::query::batch::{
    self, decode_ipc_stream, encode_ipc_stream, validate_json_request, NullPolicy, TypedBatch,
};
use crate::query::model::{resolve, QueryShape, SchemaSource};
use crate::query::oracle;
use crate::query::value::Scalar;
use crate::state::{Catalog, ServerConfig};

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub catalog: Catalog,
    pub config: std::sync::Arc<ServerConfig>,
}

const ARROW_CONTENT_TYPE: &str = "application/vnd.apache.arrow.stream";

/// Build the complete router with body limit and request-id middleware.
pub fn app(catalog: Catalog, config: ServerConfig) -> Router {
    let max_body = config.max_body_bytes;
    let state = AppState {
        catalog,
        config: std::sync::Arc::new(config),
    };
    Router::new()
        .route("/healthz", get(healthz))
        .route("/config", get(get_config))
        .route("/relations", get(list_relations))
        .route("/relations/{name}", get(get_relation).post(load_relation))
        .route("/query", post(query))
        .route("/query/continue", post(query_continue))
        .route("/verify", post(verify))
        .route("/fixtures/load", post(load_fixtures))
        .layer(middleware::from_fn(request_id_layer))
        .layer(axum::extract::DefaultBodyLimit::max(max_body))
        .with_state(state)
}

#[derive(Clone)]
struct RequestId(pub String);

async fn request_id_layer(mut req: Request<Body>, next: Next) -> Response {
    let incoming = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .filter(|s| !s.is_empty() && s.len() <= 128 && s.is_ascii());
    let rid = incoming.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

    req.extensions_mut().insert(RequestId(rid.clone()));
    let method = req.method().clone();
    let path = req.uri().path().to_string();

    let span = tracing::info_span!("http", request_id = %rid);
    let _enter = span.enter();
    let mut response = next.run(req).await;

    response
        .headers_mut()
        .insert("x-request-id", HeaderValue::try_from(&rid).unwrap());
    let decision = response
        .headers()
        .get("x-decision")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("rejected")
        .to_string();
    if response.status().is_success() {
        info!(%method, %path, status = %response.status(), %decision, "request handled");
    } else {
        warn!(%method, %path, status = %response.status(), %decision, "request rejected");
    }
    response
}

// ---------------------------------------------------------------------------
// Error responses
// ---------------------------------------------------------------------------

struct ApiError {
    rid: String,
    error: Error,
}

impl ApiError {
    fn new(rid: &str, error: Error) -> Self {
        Self {
            rid: rid.to_string(),
            error,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = match &self.error {
            Error::Validation { reason } => reason.code.http_status(),
            Error::Budget(_) => StatusCode::UNPROCESSABLE_ENTITY,
            Error::Arrow(_) | Error::Json(_) => StatusCode::BAD_REQUEST,
            Error::Io(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        let body = match &self.error {
            Error::Validation { reason } => json!({
                "request_id": self.rid,
                "decision": "rejected",
                "error": reason,
            }),
            other => json!({
                "request_id": self.rid,
                "decision": "rejected",
                "error": {"code": "internal_error", "message": other.to_string()},
            }),
        };
        warn!(
            request_id = %self.rid,
            reason = ?self.error.reason().map(|r| r.code),
            "rejecting request"
        );
        (status, [(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
    }
}

fn decision_response(rid: &str, status: StatusCode, decision: &str, mut body: Value) -> Response {
    body["request_id"] = json!(rid);
    body["decision"] = json!(decision);
    let mut resp = (status, Json(body)).into_response();
    resp.headers_mut()
        .insert("x-decision", HeaderValue::from_static(decision));
    resp
}

fn accepted(rid: &str, body: Value) -> Response {
    decision_response(rid, StatusCode::OK, "accepted", body)
}

// ---------------------------------------------------------------------------
// Read helpers
// ---------------------------------------------------------------------------

async fn read_body(rid: &str, s: &AppState, body: Body) -> Result<Vec<u8>, ApiError> {
    to_bytes(body, s.config.max_body_bytes)
        .await
        .map(|b| b.to_vec())
        .map_err(|_| {
            ApiError::new(
                rid,
                Error::validation(
                    ReasonCode::PayloadTooLarge,
                    format!("request body exceeds {} bytes", s.config.max_body_bytes),
                ),
            )
        })
}

fn parse_json<T: for<'de> Deserialize<'de>>(rid: &str, bytes: &[u8]) -> Result<T, ApiError> {
    serde_json::from_slice(bytes).map_err(|e| {
        ApiError::new(
            rid,
            Error::validation(ReasonCode::MalformedJson, e.to_string()),
        )
    })
}

// ---------------------------------------------------------------------------
// Simple handlers
// ---------------------------------------------------------------------------

async fn healthz() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

async fn get_config(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
) -> Response {
    accepted(
        &rid.0,
        json!({"config": s.config.as_ref()}),
    )
}

async fn list_relations(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
) -> Response {
    accepted(
        &rid.0,
        json!({
            "version": s.catalog.current_version(),
            "relations": s.catalog.list(),
        }),
    )
}

async fn get_relation(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> Result<Response, ApiError> {
    match s.catalog.get(&name) {
        Some(info) => Ok(accepted(
            &rid.0,
            json!({"relation": info, "version": s.catalog.current_version()}),
        )),
        None => Err(ApiError::new(
            &rid.0,
            Error::validation(
                ReasonCode::UnknownRelation,
                format!("relation {name} is not loaded"),
            ),
        )),
    }
}

// ---------------------------------------------------------------------------
// Ingestion
// ---------------------------------------------------------------------------

#[derive(Deserialize, Default)]
struct LoadQuery {
    #[serde(default)]
    null_policy: Option<NullPolicy>,
    #[serde(default)]
    replace: Option<bool>,
}

async fn load_relation(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    Path(name): Path<String>,
    QueryExtractor(params): QueryExtractor<LoadQuery>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, ApiError> {
    let bytes = read_body(&rid.0, &s, body).await?;

    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/json")
        .split(';')
        .next()
        .unwrap_or("application/json")
        .trim();

    let report = if content_type == ARROW_CONTENT_TYPE {
        let policy = params.null_policy.unwrap_or_default();
        let mut rep = decode_ipc_stream(&bytes, policy).map_err(|e| ApiError::new(&rid.0, e))?;
        rep.replace = params.replace.unwrap_or(false);
        rep
    } else {
        let mut req: batch::LoadRequest = parse_json(&rid.0, &bytes)?;
        if let Some(policy) = params.null_policy {
            req.null_policy = policy;
        }
        if let Some(replace) = params.replace {
            req.replace = replace;
        }
        validate_json_request(&name, &req).map_err(|e| ApiError::new(&rid.0, e))?
    };

    let dropped = report.dropped_null_rows;
    let outcome = s
        .catalog
        .load(&name, report)
        .map_err(|e| ApiError::new(&rid.0, e))?;
    info!(
        request_id = %rid.0,
        relation = %name,
        version = outcome.version,
        rows_loaded = outcome.rows_loaded,
        distinct_rows = outcome.distinct_rows,
        dropped_null_rows = dropped,
        replaced = outcome.replaced,
        "relation ingested"
    );
    Ok(decision_response(
        &rid.0,
        StatusCode::CREATED,
        "accepted",
        json!({"load": outcome}),
    ))
}

// ---------------------------------------------------------------------------
// Query / resume
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct QueryBody {
    query: QueryShape,
    row_limit: Option<usize>,
    /// Absent: server default. Explicit 0 disables the budget this request.
    access_budget: Option<u64>,
    #[serde(default)]
    format: Option<String>,
}

#[derive(Deserialize)]
struct ContinueBody {
    query: QueryShape,
    cursor: String,
    row_limit: Option<usize>,
    access_budget: Option<u64>,
    #[serde(default)]
    format: Option<String>,
}

fn resolve_limit(rid: &str, row_limit: Option<usize>, cfg: &ServerConfig) -> Result<usize, ApiError> {
    let limit = row_limit.unwrap_or(cfg.default_row_limit);
    if limit == 0 || limit > cfg.max_row_limit {
        return Err(ApiError::new(
            rid,
            Error::validation(
                ReasonCode::InvalidLimit,
                format!("row_limit must be between 1 and {} (got {limit})", cfg.max_row_limit),
            ),
        ));
    }
    Ok(limit)
}

fn resolve_budget(access_budget: Option<u64>, cfg: &ServerConfig) -> Option<u64> {
    match access_budget {
        Some(0) => None,
        other => other.or(cfg.default_access_budget),
    }
}

fn scalar_type_of(v: &Scalar) -> crate::query::batch::ColumnType {
    match v {
        Scalar::Int(_) => crate::query::batch::ColumnType::Int64,
        Scalar::Float(_) => crate::query::batch::ColumnType::Float64,
        Scalar::Bool(_) => crate::query::batch::ColumnType::Boolean,
        Scalar::Str(_) => crate::query::batch::ColumnType::Utf8,
        Scalar::Null => crate::query::batch::ColumnType::Int64,
    }
}

/// Column type per output variable, taken from answer values when present and
/// otherwise from the first stored atom binding the variable.
fn output_column_types(
    snapshot: &crate::state::CatalogSnapshot,
    shape: &QueryShape,
    vars: &[String],
    rows: &[leapfrog::AnswerRow],
) -> Vec<crate::query::batch::ColumnType> {
    vars.iter()
        .enumerate()
        .map(|(i, var)| {
            if let Some(row) = rows.iter().find(|r| !r.values.is_empty()) {
                return scalar_type_of(&row.values[i]);
            }
            for atom in &shape.atoms {
                if let Some(pos) = atom.vars.iter().position(|v| v == var) {
                    if let Some(cols) = snapshot.relation_columns(&atom.relation) {
                        return cols[pos].1;
                    }
                }
            }
            crate::query::batch::ColumnType::Int64
        })
        .collect()
}

fn answer_batch(
    vars: &[String],
    types: &[crate::query::batch::ColumnType],
    rows: &[leapfrog::AnswerRow],
) -> TypedBatch {
    TypedBatch {
        columns: vars
            .iter()
            .zip(types)
            .map(|(v, t)| (v.clone(), *t))
            .collect(),
        rows: rows.iter().map(|r| r.values.clone()).collect(),
    }
}

fn render_slice(
    rid: &str,
    s: &AppState,
    shape: &QueryShape,
    vars: &[String],
    outcome: leapfrog::RunOutcome,
    format: Option<&str>,
) -> Response {
    let decision = match outcome.stop {
        leapfrog::StopReason::Exhausted => "accepted",
        leapfrog::StopReason::RowLimit | leapfrog::StopReason::AccessBudget => "incomplete",
    };
    let stop = outcome.stop;
    let stats = outcome.stats;
    let snapshot = s.catalog.snapshot();
    let types = output_column_types(&snapshot, shape, vars, &outcome.rows);

    if format == Some("arrow") {
        let rows = outcome.rows.clone();
        let columns: Vec<(String, crate::query::batch::ColumnType)> = vars
            .iter()
            .zip(types.iter().copied())
            .map(|(v, t)| (v.clone(), t))
            .chain(std::iter::once((
                "_multiplicity".to_string(),
                crate::query::batch::ColumnType::Int64,
            )))
            .collect();
        let batch = TypedBatch {
            columns,
            /* placeholder replaced below */
            rows: Vec::new(),
        };
        let batch = TypedBatch {
            columns: batch.columns,
            rows: rows
                .iter()
                .map(|r| {
                    r.values
                        .iter()
                        .cloned()
                        .chain(std::iter::once(Scalar::Int(r.multiplicity as i64)))
                        .collect()
                })
                .collect(),
        };
        let bytes = match encode_ipc_stream(&batch) {
            Ok(b) => b,
            Err(e) => return ApiError::new(rid, e).into_response(),
        };
        let mut resp = (
            StatusCode::OK,
            [(header::CONTENT_TYPE, HeaderValue::from_static(ARROW_CONTENT_TYPE))],
            bytes,
        )
            .into_response();
        resp.headers_mut()
            .insert("x-decision", HeaderValue::from_static(decision));
        resp.headers_mut().insert(
            "x-stop-reason",
            HeaderValue::from_static(match stop {
                leapfrog::StopReason::Exhausted => "exhausted",
                leapfrog::StopReason::RowLimit => "row_limit",
                leapfrog::StopReason::AccessBudget => "access_budget",
            }),
        );
        return resp;
    }

    let columns: Vec<Value> = vars
        .iter()
        .zip(types)
        .map(|(v, t)| json!({"name": v, "type": t.as_str()}))
        .collect();
    let rows: Vec<Value> = outcome
        .rows
        .iter()
        .map(|r| {
            json!({
                "values": r.values,
                "multiplicity": r.multiplicity,
            })
        })
        .collect();
    let cursor = outcome.cursor.as_ref().map(Cursor::encode);
    let body = json!({
        "vars": vars,
        "columns": columns,
        "rows": rows,
        "stop_reason": stop,
        "stats": stats,
        "cursor": cursor,
    });
    decision_response(rid, StatusCode::OK, decision, body)
}

fn execute_slice(
    rid: &str,
    s: &AppState,
    shape: &QueryShape,
    params: RunParams,
    cursor: Option<&Cursor>,
    format: Option<&str>,
) -> Result<Response, ApiError> {
    let snapshot = s.catalog.snapshot();
    let pcfg = Catalog::planner_config(&s.config);
    let plan = resolve(shape, &snapshot, &pcfg).map_err(|e| ApiError::new(rid, e))?;
    let vars = plan.global_order.clone();
    let index_policy = plan.index_policy;
    let nvars = plan.var_count();

    let outcome =
        leapfrog::run(&plan, params, cursor).map_err(|e| ApiError::new(rid, e))?;
    info!(
        request_id = %rid,
        catalog_version = plan.catalog_version,
        variables = nvars,
        atoms = plan.resolved.len(),
        ?index_policy,
        stop = ?outcome.stop,
        rows = outcome.rows.len(),
        total_accesses = outcome.stats.total_accesses(),
        "query slice executed"
    );
    Ok(render_slice(rid, s, shape, &vars, outcome, format))
}

async fn query(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    body: Body,
) -> Result<Response, ApiError> {
    let bytes = read_body(&rid.0, &s, body).await?;
    let req: QueryBody = parse_json(&rid.0, &bytes)?;
    let row_limit = resolve_limit(&rid.0, req.row_limit, &s.config)?;
    let params = RunParams {
        row_limit,
        access_budget: resolve_budget(req.access_budget, &s.config),
    };
    execute_slice(&rid.0, &s, &req.query, params, None, req.format.as_deref())
}

async fn query_continue(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    body: Body,
) -> Result<Response, ApiError> {
    let bytes = read_body(&rid.0, &s, body).await?;
    let req: ContinueBody = parse_json(&rid.0, &bytes)?;
    let row_limit = resolve_limit(&rid.0, req.row_limit, &s.config)?;
    let cursor =
        Cursor::decode(&req.cursor).map_err(|e| ApiError::new(&rid.0, e))?;
    let params = RunParams {
        row_limit,
        access_budget: resolve_budget(req.access_budget, &s.config),
    };
    execute_slice(
        &rid.0,
        &s,
        &req.query,
        params,
        Some(&cursor),
        req.format.as_deref(),
    )
}

// ---------------------------------------------------------------------------
// Verification against the independent oracle
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct VerifyBody {
    query: QueryShape,
    /// Optional row limit per resume slice while driving the engine to
    /// completion; defaults to the server default.
    resume_row_limit: Option<usize>,
}

async fn verify(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    body: Body,
) -> Result<Response, ApiError> {
    let bytes = read_body(&rid.0, &s, body).await?;
    let req: VerifyBody = parse_json(&rid.0, &bytes)?;
    let snapshot = s.catalog.snapshot();
    let pcfg = Catalog::planner_config(&s.config);
    let plan = resolve(&req.query, &snapshot, &pcfg).map_err(|e| ApiError::new(&rid.0, e))?;

    // Independent reference engine. It never calls the planner and
    // deliberately materializes every partial-assignment table.
    let cap = s.config.oracle_intermediate_cap;
    let oracle_result = oracle::run_naive(&req.query, &snapshot, cap);
    let oracle_body = match oracle_result {
        Ok(outcome) => json!({
            "status": "completed",
            "rows": outcome.rows.len(),
            "multiplicity_total": outcome
                .rows
                .iter()
                .map(|(_, m)| m)
                .sum::<u64>(),
            "row_visits": outcome.row_visits,
            "max_intermediate": outcome.max_intermediate,
            "rows_json": outcome
                .rows
                .iter()
                .map(|(vals, mult)| json!({"values": vals, "multiplicity": mult}))
                .collect::<Vec<_>>(),
        }),
        Err(Error::Budget(msg)) => json!({"status": "budget_exceeded", "message": msg, "cap": cap}),
        Err(other) => return Err(ApiError::new(&rid.0, other)),
    };

    // Drive the Leapfrog engine through as many resume slices as needed.
    let row_limit = resolve_limit(&rid.0, req.resume_row_limit, &s.config)?;
    let mut engine_rows: Vec<leapfrog::AnswerRow> = Vec::new();
    let mut cursor = None;
    let mut slices = 0usize;
    let mut last_stats = leapfrog::JoinStats::default();
    loop {
        let outcome = leapfrog::run(
            &plan,
            RunParams {
                row_limit,
                access_budget: s.config.default_access_budget,
            },
            cursor.as_ref(),
        )
        .map_err(|e| ApiError::new(&rid.0, e))?;
        slices += 1;
        last_stats = outcome.stats;
        engine_rows.extend(outcome.rows);
        match outcome.stop {
            leapfrog::StopReason::Exhausted => break,
            _ => cursor = outcome.cursor,
        }
        if slices > 100_000 {
            return Err(ApiError::new(
                &rid.0,
                Error::Budget("verification did not converge after 100000 slices".into()),
            ));
        }
    }

    // Compare results and multiplicities. The expected multiset comes from
    // the oracle, never from the engine under test.
    let mut expected: Vec<(Vec<Scalar>, u64)> = Vec::new();
    let mut verdict = "match";
    let mut discrepancy = Value::Null;
    if oracle_body["status"] == "completed" {
        for item in oracle_body["rows_json"].as_array().unwrap() {
            let vals: Vec<Scalar> =
                serde_json::from_value(item["values"].clone()).unwrap();
            let mult = item["multiplicity"].as_u64().unwrap();
            expected.push((vals, mult));
        }
        let got: Vec<(Vec<Scalar>, u64)> = engine_rows
            .iter()
            .map(|r| (r.values.clone(), r.multiplicity))
            .collect();
        if got != expected {
            verdict = "mismatch";
            discrepancy = json!({
                "expected_rows": expected.len(),
                "engine_rows": got.len(),
                "first_engine_rows": got.iter().take(5).map(|(v, m)| json!({"values": v, "multiplicity": m})).collect::<Vec<_>>(),
            });
        }
    } else {
        verdict = "oracle_inconclusive";
    }

    let body = json!({
        "verdict": verdict,
        "query": req.query,
        "catalog_version": plan.catalog_version,
        "index_policy": plan.index_policy,
        "resume_slices": slices,
        "engine": {
            "distinct_rows": engine_rows.len(),
            "multiplicity_total": engine_rows.iter().map(|r| r.multiplicity).sum::<u64>(),
            "stats": last_stats,
            "total_accesses": last_stats.total_accesses(),
            "rows": engine_rows
                .iter()
                .map(|r| json!({"values": r.values, "multiplicity": r.multiplicity}))
                .collect::<Vec<_>>(),
        },
        "oracle": oracle_body,
        "discrepancy": discrepancy,
    });
    info!(
        request_id = %rid.0,
        %verdict,
        engine_rows = engine_rows.len(),
        engine_accesses = last_stats.total_accesses(),
        resume_slices = slices,
        "verification complete"
    );
    accepted(&rid.0, body)
}

// ---------------------------------------------------------------------------
// Synthetic fixture loading
// ---------------------------------------------------------------------------

#[derive(Deserialize, Default)]
struct FixtureQuery {
    #[serde(default)]
    scenario: Option<String>,
    #[serde(default)]
    replace: Option<bool>,
}

async fn load_fixtures(
    Extension(rid): Extension<RequestId>,
    State(s): State<AppState>,
    QueryExtractor(params): QueryExtractor<FixtureQuery>,
) -> Result<Response, ApiError> {
    let replace = params.replace.unwrap_or(true);
    let requested = match params.scenario {
        Some(name) => vec![name],
        None => fixtures::all_scenarios().into_iter().map(str::to_string).collect(),
    };
    let mut loaded = Vec::new();
    for scenario_name in requested {
        let scenario = fixtures::scenario(&scenario_name).ok_or_else(|| {
            ApiError::new(
                &rid.0,
                Error::validation(
                    ReasonCode::UnknownRelation,
                    format!("unknown fixture scenario {scenario_name}"),
                ),
            )
        })?;
        for relation in &scenario.relations {
            let mut report = relation.report.clone();
            report.replace = replace;
            let outcome = s
                .catalog
                .load(&relation.name, report)
                .map_err(|e| ApiError::new(&rid.0, e))?;
            loaded.push(json!(outcome));
        }
    }
    Ok(decision_response(
        &rid.0,
        StatusCode::CREATED,
        "accepted",
        json!({"loaded": loaded, "version": s.catalog.current_version()}),
    ))
}
