//! HTTP 接口（纯后端 JSON API，无界面）。

use crate::db::{CompactionReport, CrashPoint, Db};
use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
struct AppState {
    db: Arc<Db>,
}

pub fn router(db: Arc<Db>) -> Router {
    let app_state = AppState { db };
    Router::new()
        .route("/put", post(put))
        .route("/delete", post(delete))
        .route("/get", get(get_key))
        .route("/scan", get(scan))
        .route("/flush", post(flush))
        .route("/compact", post(compact))
        .route("/state", get(state))
        .with_state(app_state)
}

#[derive(Debug, Deserialize)]
struct PutReq {
    key: String,
    value: String,
}

#[derive(Debug, Serialize)]
struct SeqResp {
    seq: u64,
}

async fn put(State(s): State<AppState>, Json(req): Json<PutReq>) -> ApiResult<Json<SeqResp>> {
    let seq = run_blocking(move || s.db.put(&req.key, &req.value)).await?;
    Ok(Json(SeqResp { seq }))
}

#[derive(Debug, Deserialize)]
struct DeleteReq {
    key: String,
}

async fn delete(State(s): State<AppState>, Json(req): Json<DeleteReq>) -> ApiResult<Json<SeqResp>> {
    let seq = run_blocking(move || s.db.delete(&req.key)).await?;
    Ok(Json(SeqResp { seq }))
}

#[derive(Debug, Deserialize)]
struct KeyQuery {
    key: String,
}

#[derive(Debug, Serialize)]
struct GetResp {
    key: String,
    found: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<String>,
}

async fn get_key(State(s): State<AppState>, Query(q): Query<KeyQuery>) -> ApiResult<Json<GetResp>> {
    let key = q.key.clone();
    let value = run_blocking(move || s.db.get(&key)).await?;
    Ok(Json(GetResp {
        key: q.key,
        found: value.is_some(),
        value,
    }))
}

#[derive(Debug, Deserialize)]
struct ScanQuery {
    start: Option<String>,
    end: Option<String>,
}

#[derive(Debug, Serialize)]
struct ScanResp {
    items: Vec<(String, String)>,
}

async fn scan(State(s): State<AppState>, Query(q): Query<ScanQuery>) -> ApiResult<Json<ScanResp>> {
    let items = run_blocking(move || s.db.scan(q.start.as_deref(), q.end.as_deref())).await?;
    Ok(Json(ScanResp { items }))
}

#[derive(Debug, Serialize)]
struct FlushResp {
    segment_ids: Vec<u64>,
}

async fn flush(State(s): State<AppState>) -> ApiResult<Json<FlushResp>> {
    let segment_ids = run_blocking(move || s.db.flush()).await?;
    Ok(Json(FlushResp { segment_ids }))
}

#[derive(Debug, Deserialize, Default)]
struct CompactReq {
    /// "full"（默认）或 "range"。
    #[serde(default)]
    mode: Option<String>,
    /// range 模式：段下标区间（新 -> 旧，0 为最新段）。
    newer_idx: Option<usize>,
    older_idx: Option<usize>,
    /// 故障注入：after_new_segment_before_manifest / after_manifest_before_delete_old。
    crash: Option<String>,
}

#[derive(Debug, Serialize)]
struct CompactResp {
    report: CompactionReport,
    warning: Option<String>,
}

async fn compact(
    State(s): State<AppState>,
    Json(req): Json<CompactReq>,
) -> ApiResult<Json<CompactResp>> {
    let crash = match req.crash.as_deref() {
        None => None,
        Some(name) => Some(
            CrashPoint::parse(name)
                .ok_or_else(|| ApiError::bad_request(format!("unknown crash point: {name}")))?,
        ),
    };
    let mode = req.mode.unwrap_or_else(|| "full".to_string());
    let newer = req.newer_idx;
    let older = req.older_idx;

    if mode == "range" && (newer.is_none() || older.is_none()) {
        return Err(ApiError::bad_request(
            "range mode requires newer_idx and older_idx",
        ));
    }
    if mode != "full" && mode != "range" {
        return Err(ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: format!("unknown mode: {mode}"),
        });
    }

    let result = run_blocking(move || match mode.as_str() {
        "full" => s.db.compact_full(crash),
        "range" => s.db.compact_range(newer.unwrap(), older.unwrap(), crash),
        _ => unreachable!(),
    })
    .await;

    match result {
        Ok(report) => Ok(Json(CompactResp {
            report,
            warning: crash.map(|_| {
                "crash injected: this process must be restarted before further use".into()
            }),
        })),
        Err(e) => Err(e),
    }
}

async fn state(State(s): State<AppState>) -> ApiResult<Json<crate::db::StateSnapshot>> {
    let snap = run_blocking(move || Ok::<_, anyhow::Error>(s.db.snapshot())).await?;
    Ok(Json(snap))
}

/// DB 操作为同步且持锁，放到阻塞线程池执行，避免阻塞 tokio 运行时。
async fn run_blocking<F, T>(f: F) -> Result<T, ApiError>
where
    F: FnOnce() -> anyhow::Result<T> + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?
        .map_err(|e| ApiError::internal(format!("{e:#}")))
}

type ApiResult<T> = Result<T, ApiError>;

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn bad_request(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: msg.into(),
        }
    }
    fn internal(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: msg.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        #[derive(Serialize)]
        struct Body {
            error: String,
        }
        (
            self.status,
            Json(Body {
                error: self.message,
            }),
        )
            .into_response()
    }
}
