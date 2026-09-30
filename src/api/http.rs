//! Axum HTTP surface.
//!
//! Routes:
//! * `GET  /healthz`        — liveness + version + catalog listing
//! * `POST /v1/query`       — validate/rewrite/execute/verify a request
//!
//! Every response and log line carries the correlated request id
//! (`X-Request-Id` echoed back; generated when the caller does not supply one).

use crate::api::service::{handle, Outcome, SERVICE_BUILD_AT, SERVICE_VERSION};
use crate::api::types::{ErrorResponse, HealthResponse, QueryRequest};
use crate::catalog::Catalog;
use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, HeaderName, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use std::time::Instant;
use tracing::{error, info, warn};
use uuid::Uuid;

#[derive(Clone)]
pub struct AppState {
    pub catalog: Catalog,
}

pub fn router(catalog: Catalog) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/query", post(query))
        .with_state(AppState { catalog })
}

async fn healthz(State(state): State<AppState>) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok".to_string(),
        service_version: SERVICE_VERSION.to_string(),
        catalog_relations: state.catalog.names(),
        supported_kinds: vec!["exists", "not_exists", "scalar", "scalar_agg"],
    })
}

const REQUEST_ID_HEADER: HeaderName = HeaderName::from_static("x-request-id");

async fn query(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let started = Instant::now();
    let request_id = headers
        .get(&REQUEST_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| format!("req_{}", Uuid::new_v4()));

    info!(request_id = %request_id, step = "http:receive", bytes = body.len(), "query request received");

    // Parse JSON explicitly so malformed bodies get a categorized error.
    let request: QueryRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => {
            warn!(request_id = %request_id, step = "http:parse", error = %e, "malformed JSON body");
            let response = ErrorResponse {
                request_id: request_id.clone(),
                status: "rejected".to_string(),
                service_version: SERVICE_VERSION.to_string(),
                error: crate::api::types::ErrorView {
                    category: "Validation".to_string(),
                    code: "malformed_json".to_string(),
                    message: format!("request body is not valid JSON: {}", e),
                    at_step: "http:parse".to_string(),
                    at_outer_row: None,
                    detail: None,
                },
                steps: vec![],
                uncertainties: vec![],
            };
            return with_request_id(StatusCode::BAD_REQUEST, &request_id, Json(response)).into_response();
        }
    };

    let outcome = handle(request, request_id.clone(), &state.catalog);
    let elapsed = started.elapsed();

    match outcome {
        Outcome::Success(response) => {
            info!(
                request_id = %request_id,
                step = "http:respond",
                status = %response.status,
                rows = response.results.final_rows.as_ref().map(|r| r.row_count),
                elapsed_us = elapsed.as_micros() as u64,
                location = SERVICE_BUILD_AT,
                "request completed"
            );
            with_request_id(StatusCode::OK, &request_id, Json(response)).into_response()
        }
        Outcome::VerifiedError { status, response, results } => {
            warn!(
                request_id = %request_id,
                step = "http:respond",
                status = %response.status,
                category = %response.error.category,
                code = %response.error.code,
                outer_row = ?response.error.at_outer_row,
                equivalent = true,
                elapsed_us = elapsed.as_micros() as u64,
                "both strategies raised the same verified error"
            );
            // The verified-error payload extends the error envelope with the
            // comparison views so callers can inspect both executions.
            let mut payload = serde_json::to_value(&response).unwrap_or(serde_json::Value::Null);
            if let Some(obj) = payload.as_object_mut() {
                obj.insert("results".to_string(), serde_json::to_value(results).unwrap_or(serde_json::Value::Null));
            }
            with_request_id(StatusCode::from_u16(status).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY), &request_id, Json(payload)).into_response()
        }
        Outcome::Rejected { status, response } => {
            error!(
                request_id = %request_id,
                step = "http:respond",
                status = %response.status,
                category = %response.error.category,
                code = %response.error.code,
                at_step = %response.error.at_step,
                elapsed_us = elapsed.as_micros() as u64,
                "request rejected"
            );
            with_request_id(StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST), &request_id, Json(response)).into_response()
        }
    }
}

fn with_request_id<T>(status: StatusCode, request_id: &str, body: Json<T>) -> impl IntoResponse
where
    T: serde::Serialize,
{
    let mut response = (status, body).into_response();
    if let Ok(v) = axum::http::HeaderValue::from_str(request_id) {
        response.headers_mut().insert(&REQUEST_ID_HEADER, v);
    }
    response
}
