//! HTTP 层：路由、处理器、共享状态。

use std::sync::{Arc, Mutex, MutexGuard};

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, State};
use axum::http::header::{CONTENT_TYPE, LOCATION};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};

use crate::error::{AppError, AppResult};
use crate::models::{
    ArtifactView, BlobInfo, HistoryView, PromoteRequest, ProofRequest, RollbackRequest, Stage,
};
use crate::store::{Config, Store};

/// 共享应用状态。处理器均为非 async 锁内操作，std Mutex 即可。
#[derive(Clone, Default)]
pub struct AppState {
    pub store: Arc<Mutex<Store>>,
}

impl AppState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_config(config: Config) -> Self {
        Self {
            store: Arc::new(Mutex::new(Store::with_config(config))),
        }
    }

    fn lock(&self) -> AppResult<MutexGuard<'_, Store>> {
        self.store
            .lock()
            .map_err(|_| AppError::Conflict("internal state lock poisoned".to_string()))
    }
}

/// 构建路由器（测试与 main 共用）。
pub fn app(state: AppState) -> Router {
    Router::new()
        .route(
            "/",
            get(|| async {
                Json(serde_json::json!({ "service": "artifact-promotion", "ok": true }))
            }),
        )
        .route(
            "/health",
            get(|| async { Json(serde_json::json!({ "status": "ok" })) }),
        )
        .route("/artifacts", get(list_artifacts).post(register_artifact))
        .route("/artifacts/{id}", get(get_artifact))
        .route("/artifacts/{id}/history", get(get_history))
        .route("/artifacts/{id}/content", get(get_artifact_content))
        .route("/artifacts/{id}/proofs", post(record_proof))
        .route("/artifacts/{id}/promote", post(promote))
        .route("/artifacts/{id}/rollback", post(rollback))
        .route(
            "/artifacts/{id}/content/immutable",
            post(replace_content_rejected),
        )
        .route("/blobs/{digest}", get(get_blob_info))
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
        .with_state(state)
}

// ---------- 处理器 ----------

async fn list_artifacts(State(s): State<AppState>) -> AppResult<Json<Vec<ArtifactView>>> {
    Ok(Json(s.lock()?.list()))
}

/// 注册：请求体即制品原始内容（任意字节，application/oetet-stream）。
/// 摘要由服务端计算，客户端无法伪造绑定。
async fn register_artifact(
    State(s): State<AppState>,
    body: Bytes,
) -> AppResult<(StatusCode, Json<ArtifactView>)> {
    let view = s.lock()?.register(&body);
    Ok((StatusCode::CREATED, Json(view)))
}

async fn get_artifact(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> AppResult<Json<ArtifactView>> {
    Ok(Json(s.lock()?.view(&id)?))
}

async fn get_history(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> AppResult<Json<HistoryView>> {
    Ok(Json(s.lock()?.history(&id)?))
}

async fn get_artifact_content(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> AppResult<impl IntoResponse> {
    let (digest, content) = s.lock()?.artifact_content(&id)?;
    let mut headers = HeaderMap::new();
    headers.insert(
        CONTENT_TYPE,
        HeaderValue::from_static("application/octet-stream"),
    );
    if let Ok(v) = HeaderValue::from_str(&digest) {
        headers.insert("X-Content-Digest", v);
    }
    Ok((StatusCode::OK, headers, content))
}

async fn record_proof(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<ProofRequest>,
) -> AppResult<(StatusCode, Json<ArtifactView>)> {
    Ok((StatusCode::CREATED, Json(s.lock()?.record_proof(&id, req)?)))
}

async fn promote(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<PromoteRequest>,
) -> AppResult<(StatusCode, HeaderMap, Json<ArtifactView>)> {
    let view = s.lock()?.promote(&id, req)?;
    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&format!("/artifacts/{id}")) {
        headers.insert(LOCATION, v);
    }
    Ok((StatusCode::OK, headers, Json(view)))
}

async fn rollback(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<RollbackRequest>,
) -> AppResult<Json<ArtifactView>> {
    Ok(Json(s.lock()?.rollback(&id, req)?))
}

/// 显式表达"内容不可替换"：注册后任何替换内容的尝试一律 409。
async fn replace_content_rejected(
    State(s): State<AppState>,
    Path(id): Path<String>,
    _body: Bytes,
) -> AppResult<StatusCode> {
    // 存在性校验，保证未知制品返回 404 而非 409。
    s.lock()?.view(&id)?;
    Err(AppError::conflict(
        "artifact content is immutable after registration: content replacement is forbidden; \
         register a new artifact if the build output changed",
    ))
}

async fn get_blob_info(
    State(s): State<AppState>,
    Path(digest): Path<String>,
) -> AppResult<Json<BlobInfo>> {
    Ok(Json(s.lock()?.blob_info(&digest)?))
}

/// 供测试/演示：解析目标阶段（如需要）。
#[allow(dead_code)]
fn parse_stage(s: &str) -> AppResult<Stage> {
    Stage::parse(s).ok_or_else(|| AppError::bad_request(format!("unknown stage: {s}")))
}
