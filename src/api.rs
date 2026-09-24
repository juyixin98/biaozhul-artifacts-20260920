//! HTTP 接口与路由。
use std::collections::BTreeMap;

use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::header;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use tokio_util::io::ReaderStream;

use crate::error::{AppError, AppResult};
use crate::state::AppState;
use crate::store::{is_upload_id, UploadMeta};

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/uploads", post(create_upload))
        .route("/uploads/{upload_id}", get(get_upload))
        .route("/uploads/{upload_id}/chunks/{index}", put(put_chunk))
        .route("/uploads/{upload_id}/finish", post(finish_upload))
        .route("/objects/{object_id}", get(get_object).head(head_object))
        .with_state(state)
}

async fn healthz(State(st): State<AppState>) -> impl IntoResponse {
    Json(serde_json::json!({
        "status": "ok",
        "data_dir": st.store.data_dir().display().to_string(),
        "max_chunk_size": st.max_chunk_size,
    }))
}

// ---------- 创建会话 ----------

#[derive(Debug, Deserialize)]
pub struct CreateUploadRequest {
    /// 客户端事先算出的整体 sha256（十六进制）
    pub sha256: String,
    /// 整体字节数
    pub total_size: u64,
    /// 每块期望字节数，按块编号升序给出（最后一块可更短）。
    pub chunk_sizes: Vec<u64>,
}

#[derive(Debug, Serialize)]
struct UploadStatus {
    upload_id: String,
    sha256: String,
    total_size: u64,
    chunk_count: usize,
    chunks: BTreeMap<u32, ChunkStatus>,
    missing_chunks: Vec<u32>,
    committed: bool,
}

#[derive(Debug, Serialize)]
struct ChunkStatus {
    index: u32,
    size: u64,
    sha256: String,
}

fn status_of(meta: &UploadMeta) -> UploadStatus {
    UploadStatus {
        upload_id: meta.upload_id.clone(),
        sha256: meta.sha256.clone(),
        total_size: meta.total_size,
        chunk_count: meta.chunk_sizes.len(),
        chunks: meta
            .chunks
            .iter()
            .map(|(i, c)| {
                (
                    *i,
                    ChunkStatus {
                        index: *i,
                        size: c.size,
                        sha256: c.sha256.clone(),
                    },
                )
            })
            .collect(),
        missing_chunks: meta.missing_chunks(),
        committed: meta.committed,
    }
}

async fn create_upload(
    State(st): State<AppState>,
    Json(req): Json<CreateUploadRequest>,
) -> AppResult<Response> {
    let sha = crate::store::normalize_sha256(&req.sha256)
        .ok_or_else(|| AppError::bad_request("sha256 must be 64 hex characters"))?;
    if req.total_size == 0 {
        return Err(AppError::bad_request("total_size must be > 0"));
    }
    if req.chunk_sizes.is_empty() {
        return Err(AppError::bad_request("chunk_sizes must not be empty"));
    }
    let sum: u64 = req.chunk_sizes.iter().sum();
    if sum != req.total_size {
        return Err(AppError::bad_request(format!(
            "sum(chunk_sizes) = {sum} != total_size = {}",
            req.total_size
        )));
    }
    for (i, &sz) in req.chunk_sizes.iter().enumerate() {
        if sz == 0 {
            return Err(AppError::bad_request(format!("chunk {i} size must be > 0")));
        }
        if sz > st.max_chunk_size {
            return Err(AppError::payload_too_large(format!(
                "chunk {i} size {sz} exceeds server max chunk size {}",
                st.max_chunk_size
            )));
        }
    }

    let upload_id = uuid::Uuid::new_v4().to_string();
    let chunk_sizes: BTreeMap<u32, u64> = req
        .chunk_sizes
        .iter()
        .enumerate()
        .map(|(i, &s)| (i as u32, s))
        .collect();
    let meta = st
        .store
        .create_upload(
            upload_id.clone(),
            sha,
            req.total_size,
            chunk_sizes,
            crate::store::now_rfc3339(),
        )
        .await?;

    Ok((
        axum::http::StatusCode::CREATED,
        Json(serde_json::json!({
            "upload_id": upload_id,
            "chunk_count": meta.chunk_sizes.len(),
            "max_chunk_size": st.max_chunk_size,
            "put_chunk_url_template": format!("/uploads/{upload_id}/chunks/{{index}}"),
            "finish_url": format!("/uploads/{upload_id}/finish"),
        })),
    )
        .into_response())
}

// ---------- 查询会话 ----------

async fn get_upload(
    State(st): State<AppState>,
    Path(upload_id): Path<String>,
) -> AppResult<Json<UploadStatus>> {
    if !is_upload_id(&upload_id) {
        return Err(AppError::not_found("upload not found"));
    }
    let meta = st.store.load_meta(&upload_id).await?;
    Ok(Json(status_of(&meta)))
}

// ---------- 上传分块 ----------

#[derive(Debug, Deserialize)]
struct PutChunkQuery {
    /// 客户端声明的本分块 sha256
    sha256: Option<String>,
}

async fn put_chunk(
    State(st): State<AppState>,
    Path((upload_id, index)): Path<(String, u32)>,
    Query(q): Query<PutChunkQuery>,
    body: Body,
) -> AppResult<Response> {
    if !is_upload_id(&upload_id) {
        return Err(AppError::not_found("upload not found"));
    }
    let declared = q
        .sha256
        .and_then(|s| crate::store::normalize_sha256(&s))
        .ok_or_else(|| AppError::bad_request("query parameter sha256 must be 64 hex characters"))?;

    let lock = st.locks.acquire(&upload_id).await;
    let _guard = lock.lock().await;

    let mut meta = st.store.load_meta(&upload_id).await?;
    if meta.committed {
        // 已完成会话上的分块重传：同编号同内容幂等 200，其余 409。
        if let Some(existing) = meta.chunks.get(&index) {
            if existing.sha256 == declared {
                return Ok(Json(serde_json::json!({
                    "index": index, "sha256": existing.sha256, "size": existing.size,
                    "idempotent": true, "committed": true,
                }))
                .into_response());
            }
        }
        return Err(AppError::conflict("upload already committed"));
    }

    // axum 默认 Body 无长度上限（未使用 DefaultBodyLimit），流式读取中自行限流。
    let (idempotent, info) = st
        .store
        .put_chunk(
            &mut meta,
            index,
            &declared,
            st.max_chunk_size,
            body.into_data_stream(),
        )
        .await?;

    Ok((
        if idempotent {
            axum::http::StatusCode::OK
        } else {
            axum::http::StatusCode::CREATED
        },
        Json(serde_json::json!({
            "index": index,
            "sha256": info.sha256,
            "size": info.size,
            "idempotent": idempotent,
            "missing_chunks": meta.missing_chunks(),
        })),
    )
        .into_response())
}

// ---------- 完成 ----------

async fn finish_upload(
    State(st): State<AppState>,
    Path(upload_id): Path<String>,
) -> AppResult<Response> {
    if !is_upload_id(&upload_id) {
        return Err(AppError::not_found("upload not found"));
    }
    let lock = st.locks.acquire(&upload_id).await;
    let _guard = lock.lock().await;

    let mut meta = st.store.load_meta(&upload_id).await?;
    let outcome = st.store.commit(&mut meta).await?;
    Ok(Json(serde_json::json!({
        "upload_id": upload_id,
        "object_id": outcome.object_id,
        "object_url": format!("/objects/{}", outcome.object_id),
        "committed": true,
        "idempotent": outcome.already_committed,
    }))
    .into_response())
}

// ---------- 对象读取（仅已发布可读） ----------

async fn open_for_read(
    st: &AppState,
    object_id: &str,
) -> AppResult<(crate::store::ObjectMeta, tokio::fs::File)> {
    st.store.open_object(object_id).await
}

async fn head_object(
    State(st): State<AppState>,
    Path(object_id): Path<String>,
) -> AppResult<Response> {
    let (meta, _file) = open_for_read(&st, &object_id).await?;
    Ok((
        [
            (header::CONTENT_TYPE, "application/octet-stream".to_string()),
            (header::CONTENT_LENGTH, meta.size.to_string()),
            ("x-object-sha256".parse().unwrap(), meta.sha256),
        ],
        (),
    )
        .into_response())
}

async fn get_object(
    State(st): State<AppState>,
    Path(object_id): Path<String>,
) -> AppResult<Response> {
    let (meta, file) = open_for_read(&st, &object_id).await?;
    let stream = ReaderStream::with_capacity(file, 1024 * 1024);
    Ok((
        [
            (header::CONTENT_TYPE, "application/octet-stream".to_string()),
            (header::CONTENT_LENGTH, meta.size.to_string()),
            ("x-object-sha256".parse().unwrap(), meta.sha256),
        ],
        Body::from_stream(stream),
    )
        .into_response())
}
