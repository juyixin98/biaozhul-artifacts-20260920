pub mod model;
pub mod solver;

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use model::{ReplayRequest, ResolveRequest};
use serde_json::json;
use solver::Solver;
use std::sync::Arc;

pub struct AppState {
    pub started: std::time::Instant,
}

pub fn build_router() -> Router {
    let state = Arc::new(AppState {
        started: std::time::Instant::now(),
    });
    Router::new()
        .route("/health", get(health))
        .route("/resolve", post(resolve))
        .route("/replay", post(replay))
        .with_state(state)
}

async fn health(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "service": "depsolver",
        "uptime_secs": state.started.elapsed().as_secs(),
    }))
}

async fn resolve(Json(req): Json<ResolveRequest>) -> impl IntoResponse {
    let solver = match Solver::new(&req.registry, req.platform.clone(), req.extras.clone()) {
        Ok(s) => s,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": e.to_string()})),
            )
                .into_response();
        }
    };
    match solver.solve(&req.requirements) {
        Ok(outcome) => {
            // Attach a replayable lockfile when solved.
            match &outcome {
                solver::SolveOutcome::Solved { locked, .. } => {
                    let lockfile = model::Lockfile {
                        requirements: req.requirements.clone(),
                        platform: req.platform.clone(),
                        extras: req.extras.clone(),
                        locked: locked.clone(),
                    };
                    let mut v = serde_json::to_value(&outcome).unwrap();
                    v.as_object_mut()
                        .unwrap()
                        .insert("lockfile".into(), serde_json::to_value(lockfile).unwrap());
                    Json(v).into_response()
                }
                _ => Json(serde_json::to_value(&outcome).unwrap()).into_response(),
            }
        }
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response(),
    }
}

async fn replay(Json(req): Json<ReplayRequest>) -> impl IntoResponse {
    let lf = &req.lockfile;
    match solver::verify_lock(
        &req.registry,
        &lf.requirements,
        lf.platform.as_deref(),
        &lf.extras,
        &lf.locked,
    ) {
        Ok(errors) => {
            if errors.is_empty() {
                Json(json!({
                    "valid": true,
                    "locked": lf.locked,
                }))
                .into_response()
            } else {
                (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({"valid": false, "errors": errors})),
                )
                    .into_response()
            }
        }
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": e.to_string()})),
        )
            .into_response(),
    }
}
