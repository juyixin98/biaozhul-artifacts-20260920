//! HTTP 接口层(axum)。

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::log::LogError;
use crate::store::Store;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/logs/{name}/records", post(append_record).get(list_records))
        .route("/logs/{name}/records/{seq}", get(get_record))
        .route("/logs/{name}/status", get(log_status))
        .route("/logs/{name}/roll", post(roll))
        .with_state(state)
}

impl IntoResponse for LogError {
    fn into_response(self) -> Response {
        let status = match &self {
            LogError::InvalidName(_) => StatusCode::BAD_REQUEST,
            LogError::LogNotFound(_) | LogError::RecordNotFound(_) => StatusCode::NOT_FOUND,
            LogError::PayloadTooLarge { .. } => StatusCode::PAYLOAD_TOO_LARGE,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        (status, Json(json!({ "error": self.to_string() }))).into_response()
    }
}

async fn healthz() -> &'static str {
    "ok\n"
}

#[derive(Deserialize)]
struct AppendRequest {
    /// UTF-8 文本payload。
    payload: String,
}

#[derive(Serialize)]
struct AppendResponse {
    seq: u64,
    segment: u64,
}

async fn append_record(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(req): Json<AppendRequest>,
) -> Result<(StatusCode, Json<AppendResponse>), LogError> {
    let log = s.store.get_or_create(&name)?;
    let (seq, segment) = log.append(req.payload.as_bytes())?;
    Ok((StatusCode::CREATED, Json(AppendResponse { seq, segment })))
}

async fn get_record(
    State(s): State<AppState>,
    Path((name, seq)): Path<(String, u64)>,
) -> Result<Json<serde_json::Value>, LogError> {
    let log = s.store.get(&name)?;
    match log.get(seq)? {
        Some(bytes) => {
            let payload = String::from_utf8(bytes).map_err(|_| LogError::NonUtf8(seq))?;
            Ok(Json(json!({ "seq": seq, "payload": payload })))
        }
        None => Err(LogError::RecordNotFound(seq)),
    }
}

async fn list_records(
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, LogError> {
    let log = s.store.get(&name)?;
    let records: Vec<_> = log
        .list()?
        .iter()
        .map(|r| json!({ "seq": r.seq, "len": r.len }))
        .collect();
    Ok(Json(json!({ "records": records })))
}

async fn log_status(
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, LogError> {
    let log = s.store.get(&name)?;
    let st = log.status();
    Ok(Json(json!({
        "next_seq": st.next_seq,
        "record_count": st.record_count,
        "segments": st.segments,
        "active_segment": st.segments.last(),
    })))
}

async fn roll(
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, LogError> {
    let log = s.store.get(&name)?;
    let active = log.roll_manual()?;
    Ok(Json(json!({ "active_segment": active })))
}
