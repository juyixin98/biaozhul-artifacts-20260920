//! HTTP/JSON entry point (Axum).
//!
//! Routes:
//! - `GET  /health`            liveness + loaded relations
//! - `GET  /relations`         catalog summary (names/row counts only)
//! - `POST /join`              a paged, resumable restricted natural join
//!
//! Every response carries the generated `request_id`. Validation/execution
//! failures return a stable error `code` and a non-sensitive message, and are
//! logged as a redacted [`DecisionRecord`]. No raw row data is ever logged.

use axum::extract::rejection::JsonRejection;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::batch::RecordBatch;
use crate::diagnostics::{DecisionRecord, RequestId};
use crate::error::{ErrorCode, JoinError};
use crate::plan::{JoinRequest, RelationOccurrence};
use crate::state::AppState;
use crate::types::{Datum, NullPolicy};

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/relations", get(relations))
        .route("/join", post(join))
        .with_state(state)
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "service": "leapfrog-triejoin",
        "relations": state.relation_summary().unwrap_or_default(),
    }))
}

async fn relations(State(state): State<AppState>) -> Json<Value> {
    let summary = state.relation_summary().unwrap_or_default();
    let prepared = state.prepared_count();
    Json(json!({
        "relations": summary,
        "prepared_joins": prepared,
    }))
}

// ---------- DTOs ----------

#[derive(Debug, Deserialize)]
struct RelationDto {
    relation: String,
    variables: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct JoinBody {
    relations: Vec<RelationDto>,
    #[serde(default)]
    null_policy: Option<String>,
    #[serde(default)]
    projection: Option<Vec<String>>,
    #[serde(default)]
    limit: Option<u64>,
    #[serde(default)]
    cursor: Option<String>,
}

#[derive(Debug, Serialize)]
struct ColumnDto {
    name: String,
    r#type: String,
}

#[derive(Debug, Serialize)]
struct JoinResponse {
    request_id: String,
    status: String,
    columns: Vec<ColumnDto>,
    rows: Vec<Vec<Value>>,
    row_count: usize,
    truncated: bool,
    next_cursor: Option<String>,
    stats: Value,
}

fn datum_to_json(d: &Datum) -> Value {
    match d {
        Datum::Null => Value::Null,
        Datum::Int(i) => json!(*i),
        Datum::Text(s) => json!(s),
    }
}

fn batch_to_json(batch: &RecordBatch) -> (Vec<ColumnDto>, Vec<Vec<Value>>) {
    let columns = batch
        .columns
        .iter()
        .map(|c| ColumnDto {
            name: c.name.clone(),
            r#type: c.column.col_type().as_str().to_string(),
        })
        .collect();
    let rows = (0..batch.n_rows())
        .map(|r| {
            batch
                .columns
                .iter()
                .map(|c| datum_to_json(&c.column.datum_at(r)))
                .collect()
        })
        .collect();
    (columns, rows)
}

// ---------- handler ----------

async fn join(
    State(state): State<AppState>,
    body: std::result::Result<Json<JoinBody>, JsonRejection>,
) -> Response {
    let request_id = RequestId::generate();
    let Json(body) = match body {
        Ok(b) => b,
        Err(parse_err) => {
            return fail(
                &request_id,
                JoinError::new(
                    ErrorCode::InvalidRequestBody,
                    format!("malformed JSON body: {parse_err}"),
                ),
                json!({"stage": "parse_body"}),
            );
        }
    };

    let result = execute(&state, &request_id, body);
    match result {
        Ok((page, prepared_key_state)) => {
            let (columns, rows) = batch_to_json(&page.batch);
            let stats = json!({
                "seeks": page.stats.seeks,
                "nexts": page.stats.nexts,
                "opens": page.stats.opens,
                "ups": page.stats.ups,
                "assignments": page.stats.assignments,
                "rows_emitted": page.stats.rows_emitted,
                "per_relation_seeks": page.stats.per_relation_seeks,
                "trie_nodes": page.stats.trie_nodes,
                "null_rejected": page.stats.null_rejected,
            });
            DecisionRecord::accepted(
                &request_id,
                if page.truncated {
                    "join accepted; page truncated; cursor returned"
                } else {
                    "join accepted; final page"
                },
                json!({
                    "row_count": rows.len(),
                    "truncated": page.truncated,
                    "key_state": prepared_key_state,
                }),
            )
            .log();
            (
                StatusCode::OK,
                Json(JoinResponse {
                    request_id: request_id.to_string(),
                    status: "ok".into(),
                    columns,
                    rows,
                    row_count: page.batch.n_rows(),
                    truncated: page.truncated,
                    next_cursor: page.next_cursor,
                    stats,
                }),
            )
                .into_response()
        }
        Err(err) => fail(&request_id, err, json!({"stage": "execute"})),
    }
}

/// Plan + run one page. Returns the page and redacted key-state for diagnostics.
fn execute(
    state: &AppState,
    request_id: &RequestId,
    body: JoinBody,
) -> Result<(crate::executor::Page, Value), JoinError> {
    let null_policy = match &body.null_policy {
        None => NullPolicy::default(),
        Some(s) => NullPolicy::parse(s)?,
    };
    if body.relations.is_empty() {
        return Err(JoinError::new(
            ErrorCode::NotANaturalJoin,
            "`relations` must list at least two occurrences",
        ));
    }
    let request = JoinRequest {
        relations: body
            .relations
            .into_iter()
            .map(|r| RelationOccurrence {
                relation: r.relation,
                variables: r.variables,
            })
            .collect(),
        null_policy,
        projection: body.projection,
        // `limit` is a paging concern handled per request, not part of the plan.
        limit: None,
    };

    let prepared = state.prepare(&request).map_err(|e| {
        DecisionRecord::rejected(
            request_id,
            e.code.as_str(),
            e.message.clone(),
            json!({"relations": request.relations.len(), "null_policy": null_policy.as_str()}),
        )
        .log();
        e
    })?;

    let limit = state.config().clamp_limit(body.limit);
    let page = prepared.page(Some(limit), body.cursor.as_deref()).map_err(|e| {
        DecisionRecord::rejected(
            request_id,
            e.code.as_str(),
            e.message.clone(),
            json!({"cursor_present": body.cursor.is_some(), "limit": limit}),
        )
        .log();
        e
    })?;

    let key_state = json!({
        "vars": prepared.ts.plan.ordered_vars,
        "join_var_count": prepared.ts.plan.join_var_count,
        "relations": prepared.ts.plan.relations.len(),
        "domains": prepared
            .ts
            .domains
            .iter()
            .zip(prepared.ts.plan.ordered_vars.iter())
            .map(|(d, v)| json!({"var": v, "distinct": d.len()}))
            .collect::<Vec<_>>(),
    });
    Ok((page, key_state))
}

/// Map a domain error to an HTTP response and emit a rejection record.
fn fail(request_id: &RequestId, err: JoinError, state: Value) -> Response {
    DecisionRecord::rejected(request_id, err.code.as_str(), err.message.clone(), state).log();
    let status = match err.code {
        ErrorCode::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        _ => StatusCode::BAD_REQUEST,
    };
    let body = json!({
        "request_id": request_id.to_string(),
        "status": "error",
        "error": {
            "code": err.code.as_u16(),
            "code_id": err.code.as_str(),
            "message": err.message,
        }
    });
    (status, Json(body)).into_response()
}
