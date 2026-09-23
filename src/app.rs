use axum::body;
use axum::extract::{Path, State};
use axum::response::IntoResponse;
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde::Serialize;

use crate::error::{AppError, AppResult};
use crate::store::{ObjectMeta, PartRecord, SessionMeta, Store};

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    /// Maximum accepted request body size (per part), bytes.
    pub max_body: usize,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/uploads", post(create_upload))
        .route(
            "/uploads/:upload_id",
            get(get_upload).delete(abort_upload),
        )
        .route("/uploads/:upload_id/parts/:number", put(put_part))
        .route("/uploads/:upload_id/complete", post(complete_upload))
        .route("/objects/*key", get(get_object))
        .with_state(state)
}

async fn index() -> impl IntoResponse {
    Json(serde_json::json!({
        "service": "chunk-upload",
        "endpoints": {
            "POST /uploads": "create an upload session: {key,total_size,sha256,chunk_size?}",
            "GET /uploads/:id": "session status",
            "PUT /uploads/:id/parts/:n": "upload one part (raw body); idempotent",
            "POST /uploads/:id/complete": "assemble, verify and publish (idempotent)",
            "DELETE /uploads/:id": "abort an upload",
            "GET /objects/{key}": "read a published object (404 until published)"
        }
    }))
}

#[derive(Debug, Deserialize)]
struct CreateUploadReq {
    key: String,
    total_size: u64,
    sha256: String,
    chunk_size: Option<u64>,
}

#[derive(Serialize)]
struct SessionView {
    #[serde(flatten)]
    meta: SessionMeta,
    received_parts: Vec<u32>,
    missing_parts: Vec<u32>,
}

fn session_view(meta: SessionMeta) -> SessionView {
    let received_parts = meta
        .parts
        .iter()
        .filter(|p| !p.sha256.is_empty())
        .map(|p| p.number)
        .collect();
    let missing_parts = meta
        .parts
        .iter()
        .filter(|p| p.sha256.is_empty())
        .map(|p| p.number)
        .collect();
    SessionView {
        meta,
        received_parts,
        missing_parts,
    }
}

async fn create_upload(
    State(s): State<AppState>,
    Json(req): Json<CreateUploadReq>,
) -> AppResult<impl IntoResponse> {
    let meta = s
        .store
        .create_upload(req.key, req.total_size, req.sha256, req.chunk_size)
        .await?;
    Ok((axum::http::StatusCode::CREATED, Json(session_view(meta))))
}

async fn get_upload(
    State(s): State<AppState>,
    Path(upload_id): Path<String>,
) -> AppResult<Json<SessionView>> {
    let meta = s.store.get_upload(&upload_id).await?;
    Ok(Json(session_view(meta)))
}

async fn put_part(
    State(s): State<AppState>,
    Path((upload_id, number)): Path<(String, u32)>,
    req_body: axum::body::Body,
) -> AppResult<Json<PartRecord>> {
    let bytes = body::to_bytes(req_body, s.max_body)
        .await
        .map_err(|e| AppError::BadRequest(format!("failed reading part body: {e}")))?;
    let rec = s.store.put_part(&upload_id, number, &bytes).await?;
    Ok(Json(rec))
}

async fn complete_upload(
    State(s): State<AppState>,
    Path(upload_id): Path<String>,
) -> AppResult<(axum::http::StatusCode, Json<ObjectMeta>)> {
    let meta = s.store.complete_upload(&upload_id).await?;
    Ok((axum::http::StatusCode::CREATED, Json(meta)))
}

async fn abort_upload(
    State(s): State<AppState>,
    Path(upload_id): Path<String>,
) -> AppResult<Json<serde_json::Value>> {
    s.store.abort_upload(&upload_id).await?;
    Ok(Json(serde_json::json!({ "aborted": upload_id })))
}

async fn get_object(
    State(s): State<AppState>,
    Path(key): Path<String>,
) -> AppResult<impl IntoResponse> {
    // Axum 0.7 wildcard capture includes the leading slash.
    let key = key.trim_start_matches('/').to_string();
    let (meta, data) = s.store.read_object(&key).await?;
    let etag = format!("\"{}\"", meta.sha256);
    Ok((
        [
            ("content-type", "application/octet-stream".to_string()),
            ("x-object-sha256", meta.sha256),
            ("x-object-key", meta.key),
            ("etag", etag),
        ],
        data,
    ))
}
