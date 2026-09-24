//! HTTP API (Axum). Pure JSON, no frontend.
//!
//! | Method | Path                          | Purpose                                   |
//! |--------|-------------------------------|-------------------------------------------|
//! | GET    | `/health`                     | liveness                                  |
//! | GET    | `/images`                     | list local fixture images                 |
//! | POST   | `/rebuild/:image`             | verify + rebuild, publish on success      |
//! | GET    | `/rebuild/:image`             | latest published rebuild report           |
//! | GET    | `/rebuild/:image/file?path=`  | provenance of one final-tree path         |
//! | GET    | `/rebuild/:image/layers`      | ordered source-layer digests              |

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use tokio::sync::Mutex;

use crate::error::{AppError, AppResult};
use crate::limits::Limits;
use crate::rebuild::{RebuildReport, Workdir};

#[derive(Clone)]
pub struct AppState {
    pub workdir: Arc<Workdir>,
    pub limits: Arc<Limits>,
    /// Serialises rebuilds (local fixture backend; keeps publishing
    /// deterministic and avoids two staging trees racing for one image).
    pub rebuild_lock: Arc<Mutex<()>>,
}

pub fn router(workdir: Arc<Workdir>, limits: Arc<Limits>) -> Router {
    let state = AppState {
        workdir,
        limits,
        rebuild_lock: Arc::new(Mutex::new(())),
    };
    Router::new()
        .route("/health", get(health))
        .route("/images", get(list_images))
        .route("/rebuild/{image}", post(rebuild_image).get(get_report))
        .route("/rebuild/{image}/file", get(get_file))
        .route("/rebuild/{image}/layers", get(get_layers))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

async fn list_images(State(st): State<AppState>) -> AppResult<Json<serde_json::Value>> {
    let dir = st.workdir.images_dir();
    let mut names = Vec::new();
    if dir.is_dir() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() && entry.path().join("oci-layout").is_file() {
                names.push(entry.file_name().to_string_lossy().into_owned());
            }
        }
    }
    names.sort();
    Ok(Json(serde_json::json!({ "images": names })))
}

async fn rebuild_image(State(st): State<AppState>, Path(image): Path<String>) -> Response {
    // Blocking disk/CPU work off the async runtime.
    let _guard = st.rebuild_lock.lock().await;
    let workdir_task = st.workdir.clone();
    let workdir_report = st.workdir.clone();
    let limits = st.limits.clone();
    let result = tokio::task::spawn_blocking(move || workdir_task.rebuild(&image, &limits)).await;
    match result {
        Ok(Ok(report)) => {
            // Persist the report next to the published root.
            if let Err(e) = save_report(&workdir_report, &report) {
                tracing::error!(error = %e, "failed to persist report");
            }
            (StatusCode::OK, Json(report)).into_response()
        }
        Ok(Err(e)) => e.into_response(),
        Err(e) => AppError::Other(anyhow::anyhow!("rebuild task panicked: {e}")).into_response(),
    }
}

fn save_report(workdir: &Workdir, report: &RebuildReport) -> AppResult<()> {
    let dir = workdir.roots_dir().join(&report.image);
    std::fs::create_dir_all(&dir)?;
    let tmp = dir.join(format!(".report.tmp.{}", std::process::id()));
    std::fs::write(&tmp, serde_json::to_vec_pretty(report)?)?;
    std::fs::rename(&tmp, dir.join("latest.json"))?;
    Ok(())
}

fn load_latest_report(workdir: &Workdir, image: &str) -> AppResult<RebuildReport> {
    workdir.image_path(image)?; // validate name
    let path = workdir.roots_dir().join(image).join("latest.json");
    match std::fs::read(&path) {
        Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(AppError::NotFound(format!(
            "no successful rebuild published for image {image}"
        ))),
        Err(e) => Err(e.into()),
    }
}

async fn get_report(
    State(st): State<AppState>,
    Path(image): Path<String>,
) -> AppResult<Json<RebuildReport>> {
    Ok(Json(load_latest_report(&st.workdir, &image)?))
}

#[derive(Debug, Deserialize)]
struct FileQuery {
    path: String,
}

async fn get_file(
    State(st): State<AppState>,
    Path(image): Path<String>,
    Query(q): Query<FileQuery>,
) -> AppResult<Json<serde_json::Value>> {
    let report = load_latest_report(&st.workdir, &image)?;
    let needle = q.path.trim_start_matches('/');
    let rec = report
        .files
        .iter()
        .find(|f| f.path == needle)
        .ok_or_else(|| {
            AppError::NotFound(format!("path {needle} not present in rebuilt rootfs"))
        })?;
    Ok(Json(serde_json::to_value(rec)?))
}

async fn get_layers(
    State(st): State<AppState>,
    Path(image): Path<String>,
) -> AppResult<Json<serde_json::Value>> {
    let report = load_latest_report(&st.workdir, &image)?;
    Ok(Json(serde_json::json!({
        "image": report.image,
        "manifestDigest": report.manifest_digest,
        "layers": report.layers,
        "rootfsDigest": report.rootfs_digest,
    })))
}
