//! Axum HTTP service for the dependency solver.

use axum::{
    extract::Path,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::json;

use crate::fixtures;
use crate::lockfile;
use crate::model::{ReplayRequest, SolveRequest};

pub fn router() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/solve", post(solve))
        .route("/lock/replay", post(replay))
        .route("/fixtures/{name}", get(fixture))
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok", "service": "depres" }))
}

async fn solve(Json(req): Json<SolveRequest>) -> Response {
    Json(lockfile::solve_response(&req)).into_response()
}

async fn replay(Json(input): Json<ReplayRequest>) -> Response {
    Json(lockfile::replay(input)).into_response()
}

async fn fixture(Path(name): Path<String>) -> Response {
    match fixtures::by_name(&name) {
        Some(req) => Json(req).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({
                "error": "unknown fixture",
                "available": ["diamond", "mutex", "cycle_ok", "cycle_unsat", "optional"],
            })),
        )
            .into_response(),
    }
}
