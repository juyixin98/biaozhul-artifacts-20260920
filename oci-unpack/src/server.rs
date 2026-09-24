//! Axum router and handlers, split out from `main` so integration tests can
//! drive the HTTP API without binding a real port.

use std::sync::Arc;

use axum::{
    extract::{Path as AxumPath, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::json;

use crate::{Builds, Limits, Store};

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    pub builds: Arc<Builds>,
    pub limits: Arc<Limits>,
}

pub fn build_router(
    fixtures_dir: &std::path::Path,
    builds_dir: &std::path::Path,
    limits: Limits,
) -> anyhow::Result<Router> {
    let store = Arc::new(Store::new(fixtures_dir));
    let builds = Arc::new(Builds::new(builds_dir)?);
    Ok(Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/images", get(list_images))
        .route("/v1/images/{name}/rebuild", post(rebuild_image))
        .route("/v1/images/{name}/builds", get(list_builds))
        .with_state(AppState {
            store,
            builds,
            limits: Arc::new(limits),
        }))
}

async fn healthz() -> impl IntoResponse {
    Json(json!({"status":"ok","service":"oci-layered-whitelist-unpack"}))
}

async fn list_images(State(s): State<AppState>) -> impl IntoResponse {
    let mut images = Vec::new();
    let root = s.store.root();
    if let Ok(rd) = std::fs::read_dir(root) {
        for entry in rd.flatten() {
            if !entry.path().is_dir() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with('.') {
                continue;
            }
            let has_layout = entry.path().join("oci-layout").is_file()
                && entry.path().join("index.json").is_file();
            images.push(json!({"name": name, "valid_layout": has_layout}));
        }
    }
    images.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    Json(json!({"images": images}))
}

async fn rebuild_image(
    State(s): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> axum::response::Response {
    // Blocking CPU/disk work runs on the blocking pool so the async runtime
    // stays responsive.
    let state = s.clone();
    let result = tokio::task::spawn_blocking(move || {
        crate::rebuild(&state.store, &state.builds, &name, &state.limits)
    })
    .await;

    match result {
        Ok(Ok(built)) => (StatusCode::OK, Json(built)).into_response(),
        Ok(Err(e)) => e.into_response(),
        Err(join_err) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error":{"code":"join_error","message":join_err.to_string()}})),
        )
            .into_response(),
    }
}

async fn list_builds(
    State(s): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> axum::response::Response {
    if Store::validate_name(&name).is_err() {
        return crate::Error::BadName(name).into_response();
    }
    let dir = s.builds.dir().join(&name);
    let mut builds = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for entry in rd.flatten() {
            let n = entry.file_name().to_string_lossy().to_string();
            if n.starts_with('.') || n == "latest" {
                continue;
            }
            builds.push(n);
        }
    }
    builds.sort();
    let latest = std::fs::read_link(dir.join("latest"))
        .ok()
        .and_then(|p| p.file_name().map(|s| s.to_string_lossy().to_string()));
    Json(json!({"image": name, "builds": builds, "latest": latest})).into_response()
}
