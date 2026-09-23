//! Axum HTTP layer for the COW snapshot store.

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use serde::Deserialize;

use crate::base64;
use crate::store::{Store, StoreError};

pub type SharedStore = Arc<Store>;

pub fn app(store: SharedStore) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/healthz", get(healthz))
        .route("/stats", get(stats))
        .route("/branches", get(list_branches).post(create_branch))
        .route("/branches/:name", get(branch_detail).delete(delete_branch))
        .route(
            "/branches/:name/pages/:index",
            get(read_page).put(write_page),
        )
        .route("/fault", get(get_fault).put(put_fault).delete(delete_fault))
        .with_state(store)
}

async fn index() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "service": "page-level COW branching snapshot store",
        "endpoints": {
            "GET    /healthz": "liveness",
            "GET    /stats": "branches, live page ids, refcounts, total pages minted",
            "GET    /branches": "list snapshots",
            "POST   /branches": "{\"name\":\"b\",\"parent\":\"main\"} (omit parent for the first branch)",
            "GET    /branches/:name": "snapshot metadata",
            "DELETE /branches/:name": "delete a snapshot (shared pages survive)",
            "GET    /branches/:name/pages/:index": "read one page (base64 in data_b64)",
            "PUT    /branches/:name/pages/:index": "write one page; JSON {\"data_b64\":\"...\"} or raw bytes (Content-Type: application/octet-stream)",
            "GET    /fault": "show active crash-injection point",
            "PUT    /fault": "arm a crash point: {\"point\":\"before_manifest\"} (optional nth)",
            "DELETE /fault": "disarm fault injection"
        }
    }))
}

async fn healthz() -> &'static str {
    "ok\n"
}

async fn stats(State(s): State<SharedStore>) -> ApiResult<Json<serde_json::Value>> {
    let st = s.stats()?;
    Ok(Json(serde_json::to_value(st)?))
}

async fn list_branches(State(s): State<SharedStore>) -> ApiResult<Json<serde_json::Value>> {
    Ok(Json(serde_json::json!({ "branches": s.list_branches() })))
}

#[derive(Deserialize)]
struct CreateReq {
    name: String,
    parent: Option<String>,
}

async fn create_branch(
    State(s): State<SharedStore>,
    Json(req): Json<CreateReq>,
) -> ApiResult<(StatusCode, Json<serde_json::Value>)> {
    let out = s.create_branch(&req.name, req.parent.as_deref())?;
    Ok((StatusCode::CREATED, Json(serde_json::to_value(out)?)))
}

async fn branch_detail(
    State(s): State<SharedStore>,
    Path(name): Path<String>,
) -> ApiResult<Json<serde_json::Value>> {
    for b in s.list_branches() {
        if b.name == name {
            return Ok(Json(serde_json::to_value(b)?));
        }
    }
    Err(StoreError::NotFound(format!("branch {name}")))
}

async fn delete_branch(
    State(s): State<SharedStore>,
    Path(name): Path<String>,
) -> ApiResult<Json<serde_json::Value>> {
    s.delete_branch(&name)?;
    Ok(Json(serde_json::json!({ "deleted": name })))
}

async fn read_page(
    State(s): State<SharedStore>,
    Path((name, idxstr)): Path<(String, String)>,
) -> ApiResult<Json<serde_json::Value>> {
    let index: usize = idxstr
        .parse()
        .map_err(|_| StoreError::BadRequest(format!("index {idxstr:?} not a usize")))?;
    let page_id = s.page_id_at(&name, index)?;
    match s.read_page(&name, index)? {
        None => Ok(Json(serde_json::json!({
            "branch": name, "index": index, "present": false, "page_id": 0, "size": 0
        }))),
        Some(bytes) => Ok(Json(serde_json::json!({
            "branch": name,
            "index": index,
            "present": true,
            "page_id": page_id,
            "size": bytes.len(),
            "data_b64": base64::encode(&bytes)
        }))),
    }
}

#[derive(Deserialize)]
struct WriteBody {
    data_b64: String,
}

async fn write_page(
    State(s): State<SharedStore>,
    Path((name, idxstr)): Path<(String, String)>,
    headers: HeaderMap,
    body: Bytes,
) -> ApiResult<(StatusCode, Json<serde_json::Value>)> {
    let index: usize = idxstr
        .parse()
        .map_err(|_| StoreError::BadRequest(format!("index {idxstr:?} not a usize")))?;
    let data = match headers.get("content-type").and_then(|v| v.to_str().ok()) {
        Some(ct) if ct.contains("application/json") => {
            let parsed: WriteBody = serde_json::from_slice(&body)?;
            base64::decode(&parsed.data_b64).map_err(StoreError::BadRequest)?
        }
        _ => body.to_vec(),
    };
    let out = s.write_page(&name, index, &data)?;
    Ok((StatusCode::CREATED, Json(serde_json::to_value(out)?)))
}

async fn get_fault(State(s): State<SharedStore>) -> ApiResult<Json<serde_json::Value>> {
    Ok(Json(serde_json::json!({ "fault": s.get_fault()? })))
}

#[derive(Deserialize)]
struct FaultReq {
    point: String,
    #[serde(default)]
    nth: Option<u64>,
}

async fn put_fault(
    State(s): State<SharedStore>,
    Json(req): Json<FaultReq>,
) -> ApiResult<Json<serde_json::Value>> {
    let directive = match req.nth {
        Some(n) => format!("{}:{n}", req.point),
        None => req.point,
    };
    s.set_fault(Some(&directive))?;
    Ok(Json(serde_json::json!({ "armed": directive })))
}

async fn delete_fault(State(s): State<SharedStore>) -> ApiResult<Json<serde_json::Value>> {
    s.set_fault(None)?;
    Ok(Json(serde_json::json!({ "armed": null })))
}

// ---------- error handling ----------

type ApiResult<T> = Result<T, StoreError>;

impl IntoResponse for StoreError {
    fn into_response(self) -> Response {
        let (status, kind) = match &self {
            StoreError::Io(_) | StoreError::Corrupt(_) => (StatusCode::INTERNAL_SERVER_ERROR, "io"),
            StoreError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            StoreError::Conflict(_) => (StatusCode::CONFLICT, "conflict"),
            StoreError::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
        };
        let body = Json(serde_json::json!({ "error": kind, "message": self.to_string() }));
        (status, body).into_response()
    }
}
