//! HTTP API (Axum).
//!
//! ```text
//! Objects / manifests
//!   POST   /blobs                       raw body            -> {hash,size}
//!   GET    /objects/{hash}             raw bytes
//!   POST   /manifests                  {"manifests":[...],"blobs":[...]}
//!   GET    /manifests/{hash}           manifest JSON
//!
//! Roots
//!   GET    /roots
//!   GET    /roots/{name}
//!   PUT    /roots/{name}               {"manifest":"<hash>"}
//!   DELETE /roots/{name}
//!
//! Staged uploads (independent retention window)
//!   POST   /uploads                    {"manifest":{...},"retention_secs":N?}
//!   GET    /uploads
//!   POST   /uploads/{id}/complete      {"root":"<name>"}?
//!   POST   /uploads/{id}/abort
//!
//! GC / introspection
//!   POST   /gc
//!   GET    /status
//!   GET    /reachable
//! ```

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::store::{Manifest, Store, StoreError};

pub fn router(store: Store) -> Router {
    Router::new()
        .route("/blobs", post(put_blob))
        .route("/objects/:hash", get(get_object))
        .route("/manifests", post(put_manifest))
        .route("/manifests/:hash", get(get_manifest))
        .route("/roots", get(list_roots))
        .route("/roots/:name", get(get_root).put(put_root).delete(delete_root))
        .route(
            "/uploads",
            post(start_upload).get(list_uploads),
        )
        .route("/uploads/:id/complete", post(complete_upload))
        .route("/uploads/:id/abort", post(abort_upload))
        .route("/gc", post(run_gc))
        .route("/status", get(status))
        .route("/reachable", get(reachable))
        .with_state(store)
}

struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::NotFound => ApiError(StatusCode::NOT_FOUND, e.to_string()),
            StoreError::MissingRefs(_) => ApiError(StatusCode::UNPROCESSABLE_ENTITY, e.to_string()),
            StoreError::UploadExpired => ApiError(StatusCode::GONE, e.to_string()),
            StoreError::Io(msg) => ApiError(StatusCode::BAD_REQUEST, msg),
        }
    }
}

// ---- objects ---------------------------------------------------------------

async fn put_blob(State(store): State<Store>, body: Bytes) -> Result<Json<serde_json::Value>, ApiError> {
    let hash = store.put_blob(&body)?;
    Ok(Json(json!({ "hash": hash, "size": body.len() })))
}

async fn get_object(
    State(store): State<Store>,
    Path(hash): Path<String>,
) -> Result<Response, ApiError> {
    let bytes = store.get_object(&hash)?;
    let content_type = if store.is_manifest(&hash) {
        "application/json"
    } else {
        "application/octet-stream"
    };
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, content_type)],
        bytes,
    )
        .into_response())
}

async fn put_manifest(
    State(store): State<Store>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    // Validate JSON shape first for a friendly error.
    let _: Manifest = serde_json::from_slice(&body).map_err(|e| {
        ApiError(
            StatusCode::BAD_REQUEST,
            format!("invalid manifest JSON: {e}"),
        )
    })?;
    let hash = store.put_manifest_bytes(&body)?;
    Ok(Json(json!({ "hash": hash })))
}

async fn get_manifest(
    State(store): State<Store>,
    Path(hash): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let bytes = store.get_object(&hash)?;
    if !store.is_manifest(&hash) {
        return Err(ApiError(
            StatusCode::NOT_FOUND,
            "hash exists but is not a manifest".into(),
        ));
    }
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok(Json(value))
}

// ---- roots -----------------------------------------------------------------

async fn list_roots(State(store): State<Store>) -> Json<serde_json::Value> {
    Json(json!({ "roots": store.roots() }))
}

async fn get_root(
    State(store): State<Store>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let roots = store.roots();
    match roots.get(&name) {
        Some(h) => Ok(Json(json!({ "root": name, "manifest": h }))),
        None => Err(ApiError(StatusCode::NOT_FOUND, "no such root".into())),
    }
}

#[derive(Deserialize)]
struct PutRootReq {
    manifest: String,
}

async fn put_root(
    State(store): State<Store>,
    Path(name): Path<String>,
    Json(req): Json<PutRootReq>,
) -> Result<Json<serde_json::Value>, ApiError> {
    store.set_root(&name, &req.manifest)?;
    Ok(Json(json!({ "root": name, "manifest": req.manifest })))
}

async fn delete_root(
    State(store): State<Store>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let existed = store.delete_root(&name)?;
    Ok(Json(json!({ "root": name, "deleted": existed })))
}

// ---- staged uploads --------------------------------------------------------

#[derive(Deserialize)]
struct StartUploadReq {
    manifest: Manifest,
    #[serde(default)]
    retention_secs: Option<u64>,
}

#[derive(Serialize)]
struct UploadView {
    upload_id: String,
    manifest: String,
    created_unix: u64,
    retention_secs: u64,
    expires_unix: u64,
}

async fn start_upload(
    State(store): State<Store>,
    Json(req): Json<StartUploadReq>,
) -> Result<Json<UploadView>, ApiError> {
    let rec = store.start_upload(&req.manifest, req.retention_secs)?;
    Ok(Json(UploadView {
        upload_id: rec.id,
        manifest: rec.manifest,
        expires_unix: rec.created_unix.saturating_add(rec.retention_secs),
        created_unix: rec.created_unix,
        retention_secs: rec.retention_secs,
    }))
}

async fn list_uploads(State(store): State<Store>) -> Json<serde_json::Value> {
    let now = crate::store::Store::now_unix_pub();
    let ups: Vec<serde_json::Value> = store
        .uploads()
        .into_iter()
        .map(|u| {
            json!({
                "upload_id": u.id,
                "manifest": u.manifest,
                "created_unix": u.created_unix,
                "retention_secs": u.retention_secs,
                "expires_unix": u.created_unix.saturating_add(u.retention_secs),
                "expired": now >= u.created_unix.saturating_add(u.retention_secs),
            })
        })
        .collect();
    Json(json!({ "uploads": ups }))
}

#[derive(Deserialize)]
struct CompleteUploadReq {
    #[serde(default)]
    root: Option<String>,
}

async fn complete_upload(
    State(store): State<Store>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    let root = if body.is_empty() {
        None
    } else {
        let req: CompleteUploadReq = serde_json::from_slice(&body).map_err(|e| {
            ApiError(StatusCode::BAD_REQUEST, format!("invalid body: {e}"))
        })?;
        req.root
    };
    let manifest = store.complete_upload(&id, root.as_deref())?;
    Ok(Json(json!({ "upload_id": id, "manifest": manifest, "root": root })))
}

async fn abort_upload(
    State(store): State<Store>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    store.abort_upload(&id)?;
    Ok(Json(json!({ "upload_id": id, "aborted": true })))
}

// ---- GC / introspection ----------------------------------------------------

async fn run_gc(State(store): State<Store>) -> Json<crate::gc::GcReport> {
    Json(store.gc())
}

async fn status(State(store): State<Store>) -> Json<serde_json::Value> {
    let uploads = store.uploads();
    let now = crate::store::Store::now_unix_pub();
    let live = uploads
        .iter()
        .filter(|u| now < u.created_unix.saturating_add(u.retention_secs))
        .count();
    Json(json!({
        "objects": store.object_count(),
        "roots": store.roots().len(),
        "uploads_total": uploads.len(),
        "uploads_live": live,
        "gc_generation": store.gc_generation(),
    }))
}

async fn reachable(State(store): State<Store>) -> Json<serde_json::Value> {
    let mut closure: Vec<String> = store.reachable_closure().into_iter().collect();
    closure.sort();
    Json(json!({ "reachable": closure }))
}
