//! HTTP interface exposing the content-addressed store.
//!
//! See `README.md` for the full request catalogue. Summary:
//!
//! | method | path                         | meaning                                   |
//! |--------|------------------------------|-------------------------------------------|
//! | POST   | `/blocks`                    | store a raw data block                    |
//! | POST   | `/manifests`                 | store `{"refs":[...]}`                    |
//! | GET    | `/objects`                   | list hashes                               |
//! | GET    | `/objects/{hash}`            | fetch raw object bytes                    |
//! | GET    | `/objects/{hash}/meta`       | object kind metadata                      |
//! | POST   | `/uploads`                   | begin an upload session                   |
//! | POST   | `/uploads/{id}/blocks`       | put a block into a session                |
//! | POST   | `/uploads/{id}/manifests`    | put a manifest into a session             |
//! | POST   | `/uploads/{id}/complete`     | finish (grace retention starts)           |
//! | POST   | `/uploads/{id}/abort`        | abort (same retention, no publish intent) |
//! | GET    | `/roots`                     | list roots                                |
//! | PUT    | `/roots/{name}`              | publish/replace a root `{"hash":"..."}`   |
//! | DELETE | `/roots/{name}`              | delete a root                             |
//! | POST   | `/gc`                        | run a garbage-collection pass             |

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::store::{Error, Store};

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
}

pub fn router(store: Store) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/", get(health))
        .route("/blocks", post(put_block))
        .route("/manifests", post(put_manifest))
        .route("/objects", get(list_objects))
        .route("/objects/:hash", get(get_object))
        .route("/objects/:hash/meta", get(get_meta))
        .route("/uploads", post(begin_upload))
        .route("/uploads/:id/blocks", post(upload_block))
        .route("/uploads/:id/manifests", post(upload_manifest))
        .route("/uploads/:id/complete", post(complete_upload))
        .route("/uploads/:id/abort", post(abort_upload))
        .route("/roots", get(list_roots))
        .route("/roots/:name", put(publish_root).delete(delete_root))
        .route("/gc", post(run_gc))
        .with_state(state)
}

async fn health(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({
        "status": "ok",
        "objects": s.store.list_objects().len(),
        "upload_retention_secs": s.store.retention_secs(),
    }))
}

// ---- request / response bodies -------------------------------------------

#[derive(Deserialize)]
struct PublishBody {
    hash: String,
}

#[derive(Deserialize)]
struct ManifestBody {
    #[serde(default)]
    refs: Vec<String>,
}

#[derive(Serialize)]
struct HashOut {
    hash: String,
    kind: &'static str,
}

#[derive(Serialize)]
struct UploadBegin {
    upload_id: String,
}

// ---- handlers -------------------------------------------------------------

async fn put_block(State(s): State<AppState>, body: Bytes) -> Result<Json<HashOut>> {
    let hash = s.store.put_block(&body)?;
    Ok(Json(HashOut { hash, kind: "block" }))
}

async fn put_manifest(
    State(s): State<AppState>,
    Json(req): Json<ManifestBody>,
) -> Result<Json<HashOut>> {
    let hash = s.store.put_manifest(&req.refs)?;
    Ok(Json(HashOut { hash, kind: "manifest" }))
}

async fn list_objects(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({ "objects": s.store.list_objects() }))
}

async fn get_object(
    State(s): State<AppState>,
    Path(hash): Path<String>,
) -> Result<Response> {
    match s.store.get(&hash)? {
        Some(bytes) => Ok(Response::builder()
            .header("content-type", "application/octet-stream")
            .header("x-object-kind", kind_str(&s.store, &hash))
            .body(axum::body::Body::from(bytes))
            .unwrap()),
        None => Err(AppError(Error::ObjectNotFound(hash))),
    }
}

async fn get_meta(State(s): State<AppState>, Path(hash): Path<String>) -> Result<Json<serde_json::Value>> {
    let kind = s.store.kind_of(&hash).ok_or(Error::ObjectNotFound(hash.clone()))?;
    let size = s.store.get(&hash)?.map(|b| b.len()).unwrap_or(0);
    Ok(Json(json!({ "hash": hash, "kind": kind_as_str(kind), "size": size })))
}

async fn begin_upload(State(s): State<AppState>) -> Json<UploadBegin> {
    Json(UploadBegin { upload_id: s.store.begin_upload() })
}

async fn upload_block(
    State(s): State<AppState>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<HashOut>> {
    let hash = s.store.put_for_upload(&id, &body)?;
    Ok(Json(HashOut { hash, kind: "block" }))
}

async fn upload_manifest(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<ManifestBody>,
) -> Result<Json<HashOut>> {
    let hash = s.store.put_manifest_for_upload(&id, &req.refs)?;
    Ok(Json(HashOut { hash, kind: "manifest" }))
}

async fn complete_upload(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<crate::store::UploadSummary>> {
    Ok(Json(s.store.finalize_upload(&id, false)?))
}

async fn abort_upload(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<crate::store::UploadSummary>> {
    Ok(Json(s.store.finalize_upload(&id, true)?))
}

async fn list_roots(State(s): State<AppState>) -> Json<serde_json::Value> {
    let roots = s.store.list_roots().await;
    Json(json!({ "roots": roots }))
}

async fn publish_root(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(req): Json<PublishBody>,
) -> Result<Json<serde_json::Value>> {
    s.store.publish_root(&name, &req.hash).await?;
    Ok(Json(json!({ "root": name, "hash": req.hash, "published": true })))
}

async fn delete_root(
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>> {
    let existed = s.store.delete_root(&name).await?;
    Ok(Json(json!({ "root": name, "deleted": existed })))
}

async fn run_gc(State(s): State<AppState>) -> Result<Json<crate::store::GcReport>> {
    Ok(Json(s.store.gc().await?))
}

// ---- helpers --------------------------------------------------------------

fn kind_str(s: &Store, hash: &str) -> String {
    s.kind_of(hash).map(kind_as_str).unwrap_or("block").to_string()
}

fn kind_as_str(k: crate::store::ObjectKind) -> &'static str {
    match k {
        crate::store::ObjectKind::Block => "block",
        crate::store::ObjectKind::Manifest => "manifest",
    }
}

/// Map store errors onto HTTP status codes.
impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            Error::InvalidHash(_) | Error::InvalidManifest(_) | Error::Json(_) => {
                (StatusCode::BAD_REQUEST, self.to_string())
            }
            Error::UploadStillOpen(_) => (StatusCode::CONFLICT, self.to_string()),
            Error::ObjectNotFound(_) | Error::UploadNotFound(_) | Error::MissingDependency(_) => {
                (StatusCode::NOT_FOUND, self.to_string())
            }
            Error::Io(_) => (StatusCode::INTERNAL_SERVER_ERROR, self.to_string()),
        };
        (status, Json(json!({ "error": message }))).into_response()
    }
}

/// Adapter so handler `Result<T, Error>` becomes a response.
impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        self.0.into_response()
    }
}

struct AppError(Error);

impl<T> From<T> for AppError
where
    T: Into<Error>,
{
    fn from(e: T) -> Self {
        AppError(e.into())
    }
}

type Result<T> = std::result::Result<T, AppError>;
