//! seglog —— 分段追加日志崩溃恢复服务 (纯后端 HTTP API)。
//!
//! 启动:
//!
//! ```bash
//! cargo run --release
//! # 可选: SEGLOG_DATA_DIR=./data SEGLOG_SEGMENT_BYTES=4194304 SEGLOG_BIND=0.0.0.0:8080
//! ```
//!
//! 另有一个隐藏子命令 `crash-worker`, 仅供集成测试做真实进程崩溃注入。

mod crash;
mod crc32;
mod log;
mod marker;

use std::collections::HashMap;
use std::env;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::extract::{Path as AxumPath, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::log::{Error as LogError, SegmentLog};

struct AppState {
    data_dir: PathBuf,
    segment_bytes: u64,
    logs: Mutex<HashMap<String, Arc<SegmentLog>>>,
}

impl AppState {
    /// 打开(必要时创建)具名日志。名字只允许 `[A-Za-z0-9_-]+`, 防止路径穿越。
    fn open_named(&self, name: &str) -> Result<Arc<SegmentLog>, ApiError> {
        if name.is_empty()
            || name.len() > 128
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err(ApiError::bad_request("invalid log name; use [A-Za-z0-9_-]{1,128}"));
        }
        let mut cache = self.logs.lock().unwrap();
        if let Some(l) = cache.get(name) {
            return Ok(l.clone());
        }
        let dir = self.data_dir.join(name);
        match SegmentLog::open(&dir, self.segment_bytes) {
            Ok(l) => {
                let l = Arc::new(l);
                cache.insert(name.to_string(), l.clone());
                Ok(l)
            }
            Err(LogError::Corrupt(msg)) => Err(ApiError::corrupt(msg)),
            Err(e) => Err(ApiError::internal(e)),
        }
    }
}

// ---------------------------------------------------------------------------
// HTTP 处理
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct AppendJson {
    /// UTF-8 文本 (优先)。
    #[serde(default)]
    data: Option<String>,
    /// 或 base64 原始字节。
    #[serde(default)]
    payload_base64: Option<String>,
}

#[derive(Serialize)]
struct AppendResp {
    seq: u64,
    next_seq: u64,
    bytes: usize,
}

async fn append_record(
    State(state): State<Arc<AppState>>,
    AxumPath(name): AxumPath<String>,
    headers: header::HeaderMap,
    body: Bytes,
) -> Result<Json<AppendResp>, ApiError> {
    let slog = state.open_named(&name)?;

    let payload: Vec<u8> = match headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()) {
        Some(ct) if ct.contains("application/json") => {
            let parsed: AppendJson = serde_json::from_slice(&body)
                .map_err(|e| ApiError::bad_request(format!("invalid json: {e}")))?;
            match (parsed.data, parsed.payload_base64) {
                (Some(text), _) => text.into_bytes(),
                (None, Some(b64)) => B64
                    .decode(b64.trim())
                    .map_err(|e| ApiError::bad_request(format!("invalid base64: {e}")))?,
                (None, None) => {
                    return Err(ApiError::bad_request("json body needs `data` or `payload_base64`"))
                }
            }
        }
        // 非 JSON: 请求体原样作为一条记录的字节。
        _ => body.to_vec(),
    };

    // 阻塞式 fsync 写放到阻塞线程池, 避免拖住 async 运行时。
    let slog2 = slog.clone();
    let len = payload.len();
    let seq = tokio::task::spawn_blocking(move || slog2.append(&payload))
        .await
        .map_err(|e| ApiError::internal(e))??;

    Ok(Json(AppendResp {
        seq,
        next_seq: seq.saturating_add(1),
        bytes: len,
    }))
}

#[derive(Serialize)]
struct RecordJson {
    seq: u64,
    /// base64 编码的载荷 (日志可能存任意字节)。
    payload_base64: String,
    /// 若载荷是合法 UTF-8, 顺带给出文本形式, 便于人工查看。
    text: Option<String>,
}

#[derive(Serialize)]
struct ReadResp {
    log: String,
    count: usize,
    next_seq: u64,
    records: Vec<RecordJson>,
}

async fn read_records(
    State(state): State<Arc<AppState>>,
    AxumPath(name): AxumPath<String>,
) -> Result<Json<ReadResp>, ApiError> {
    let slog = state.open_named(&name)?;
    let slog2 = slog.clone();
    let records = tokio::task::spawn_blocking(move || slog2.read_all())
        .await
        .map_err(|e| ApiError::internal(e))??;
    let next_seq = slog.next_seq();
    Ok(Json(ReadResp {
        count: records.len(),
        next_seq,
        records: records
            .into_iter()
            .map(|r| RecordJson {
                seq: r.seq,
                text: String::from_utf8(r.payload.clone()).ok(),
                payload_base64: B64.encode(&r.payload),
            })
            .collect(),
        log: name,
    }))
}

#[derive(Serialize)]
struct StatusResp {
    log: String,
    persisted_next_seq: u64,
    durable_last_seq: Option<u64>,
    record_count: usize,
    segments: u64,
}

async fn log_status(
    State(state): State<Arc<AppState>>,
    AxumPath(name): AxumPath<String>,
) -> Result<Json<StatusResp>, ApiError> {
    let slog = state.open_named(&name)?;
    let info = tokio::task::spawn_blocking(move || {
        let segments = slog.segment_count()?;
        let records = slog.read_all()?;
        Ok::<(u64, Vec<crate::log::Record>, u64), LogError>((segments, records, slog.next_seq()))
    })
    .await
    .map_err(|e| ApiError::internal(e))??;
    let (segments, records, next_seq) = info;
    Ok(Json(StatusResp {
        durable_last_seq: records.last().map(|r| r.seq),
        record_count: records.len(),
        segments,
        persisted_next_seq: next_seq,
        log: name,
    }))
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

// ---------------------------------------------------------------------------
// 错误映射
// ---------------------------------------------------------------------------

enum ApiError {
    BadRequest(String),
    Corrupt(String),
    PayloadTooLarge(String),
    Internal(String),
}

impl ApiError {
    fn bad_request(msg: impl Into<String>) -> Self {
        ApiError::BadRequest(msg.into())
    }
    fn corrupt(msg: impl Into<String>) -> Self {
        ApiError::Corrupt(msg.into())
    }
    fn internal(e: impl std::fmt::Display) -> Self {
        ApiError::Internal(e.to_string())
    }
}

impl From<LogError> for ApiError {
    fn from(e: LogError) -> Self {
        match e {
            LogError::PayloadTooLarge { len, max } => ApiError::PayloadTooLarge(format!(
                "payload length {len} exceeds maximum {max}"
            )),
            LogError::Corrupt(msg) => ApiError::Corrupt(msg),
            other => ApiError::internal(other),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message) = match self {
            ApiError::BadRequest(m) => (StatusCode::BAD_REQUEST, "bad_request", m),
            ApiError::PayloadTooLarge(m) => (StatusCode::PAYLOAD_TOO_LARGE, "payload_too_large", m),
            // 中段损坏: 500 + 明确错误体, 绝不静默跳过继续服务。
            ApiError::Corrupt(m) => (StatusCode::INTERNAL_SERVER_ERROR, "log_corrupt", m),
            ApiError::Internal(m) => (StatusCode::INTERNAL_SERVER_ERROR, "internal", m),
        };
        let body = Json(serde_json::json!({ "error": code, "message": message }));
        (status, body).into_response()
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    let mut args = env::args().skip(1);
    if let Some(cmd) = args.next() {
        if cmd == "crash-worker" {
            // 测试专用子进程入口。
            crash::run_worker(crash::parse_worker_args(args));
        } else {
            eprintln!("usage: seglog [crash-worker ...]  (unknown argument: {cmd})");
            std::process::exit(2);
        }
    }

    let data_dir = PathBuf::from(env::var("SEGLOG_DATA_DIR").unwrap_or_else(|_| "./data".to_string()));
    let segment_bytes = env::var("SEGLOG_SEGMENT_BYTES")
        .ok()
        .map(|s| s.parse::<u64>().expect("SEGLOG_SEGMENT_BYTES must be a number"))
        .unwrap_or(4 * 1024 * 1024);
    let bind = env::var("SEGLOG_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let addr: SocketAddr = bind.parse().expect("SEGLOG_BIND must be socket addr");

    std::fs::create_dir_all(&data_dir).expect("create data dir");

    let state = Arc::new(AppState {
        data_dir: data_dir.clone(),
        segment_bytes,
        logs: Mutex::new(HashMap::new()),
    });

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/logs/{name}", get(log_status))
        .route("/logs/{name}/records", get(read_records).post(append_record))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind listener");
    let local = listener.local_addr().expect("local addr");
    eprintln!(
        "[seglog] listening on http://{local}  data_dir={}  segment_bytes={segment_bytes}",
        Path::new(&data_dir).display()
    );
    axum::serve(listener, app).await.expect("server error");
}
