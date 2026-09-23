//! Axum HTTP 服务。
//!
//! 所有磁盘操作是阻塞的，统一放到 `tokio::task::spawn_blocking` 中执行，
//! 避免阻塞异步运行时。索引本体由 `Mutex<Index>` 串行化。

use std::sync::{Arc, Mutex};

use axum::{
    body::Bytes,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::errors::IndexError;
use crate::index::Index;

#[derive(Clone)]
pub struct AppState {
    pub index: Arc<Mutex<Index>>,
}

/// 统一错误响应体。
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: String,
    pub kind: &'static str,
}

fn map_error(e: &IndexError) -> (StatusCode, &'static str) {
    match e {
        IndexError::KeyTooLong { .. } | IndexError::ValueTooLong { .. } | IndexError::EmptyKey => {
            (StatusCode::BAD_REQUEST, "bad_request")
        }
        IndexError::NotFound => (StatusCode::NOT_FOUND, "not_found"),
        IndexError::BucketCapacityExhausted { .. } => {
            // 507 Insufficient Storage：桶满且分裂无法解决
            (
                StatusCode::INSUFFICIENT_STORAGE,
                "bucket_capacity_exhausted",
            )
        }
        IndexError::HashModeMismatch { .. } | IndexError::Corrupt(_) => {
            (StatusCode::INTERNAL_SERVER_ERROR, "storage_state_error")
        }
        IndexError::Io(_) => (StatusCode::INTERNAL_SERVER_ERROR, "io_error"),
    }
}

fn err_response(e: IndexError) -> Response {
    let (status, kind) = map_error(&e);
    let body = ErrorBody {
        error: e.to_string(),
        kind,
    };
    (status, Json(body)).into_response()
}

/// 在阻塞线程池中执行闭包。
async fn blocking<F, T>(state: &AppState, f: F) -> std::result::Result<T, IndexError>
where
    F: FnOnce(&mut Index) -> crate::errors::Result<T> + Send + 'static,
    T: Send + 'static,
{
    let idx = state.index.clone();
    tokio::task::spawn_blocking(move || {
        let mut guard = idx.lock().expect("索引锁中毒");
        f(&mut guard)
    })
    .await
    .expect("spawn_blocking join")
}

// ---------------------------------------------------------------- 请求/响应

#[derive(Debug, Deserialize)]
pub struct PutRequest {
    pub key: String,
    /// 值接受 JSON 字符串（推荐，可放任意字节的 base64/utf8），缺省为空串。
    #[serde(default)]
    pub value: String,
}

#[derive(Debug, Serialize)]
pub struct PutResponse {
    pub ok: bool,
    pub key: String,
}

#[derive(Debug, Deserialize)]
pub struct KeyBody {
    pub key: String,
}

#[derive(Debug, Serialize)]
pub struct GetResponse {
    pub key: String,
    pub value: String,
}

#[derive(Debug, Serialize)]
pub struct DeleteResponse {
    pub key: String,
    pub existed: bool,
}

// ---------------------------------------------------------------- 处理器

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

async fn put(
    State(state): State<AppState>,
    body: std::result::Result<Json<PutRequest>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Json(req) = match body {
        Ok(r) => r,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(ErrorBody {
                    error: format!("请求体不是合法 JSON：{e}"),
                    kind: "bad_request",
                }),
            )
                .into_response();
        }
    };
    let key = req.key.clone();
    let value = req.value.clone();
    match blocking(&state, move |idx| idx.put(key.as_bytes(), value.as_bytes())).await {
        Ok(()) => Json(PutResponse {
            ok: true,
            key: req.key,
        })
        .into_response(),
        Err(e) => err_response(e),
    }
}

async fn get_key(
    State(state): State<AppState>,
    body: std::result::Result<Json<KeyBody>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Json(req) = match body {
        Ok(r) => r,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(ErrorBody {
                    error: format!("请求体不是合法 JSON：{e}"),
                    kind: "bad_request",
                }),
            )
                .into_response();
        }
    };
    let key = req.key.clone();
    match blocking(&state, move |idx| idx.get(key.as_bytes())).await {
        Ok(Some(v)) => Json(GetResponse {
            key: req.key,
            value: String::from_utf8_lossy(&v).into_owned(),
        })
        .into_response(),
        Ok(None) => (
            StatusCode::NOT_FOUND,
            Json(ErrorBody {
                error: format!("键 {} 不存在", req.key),
                kind: "not_found",
            }),
        )
            .into_response(),
        Err(e) => err_response(e),
    }
}

async fn delete_key(
    State(state): State<AppState>,
    body: std::result::Result<Json<KeyBody>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Json(req) = match body {
        Ok(r) => r,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(ErrorBody {
                    error: format!("请求体不是合法 JSON：{e}"),
                    kind: "bad_request",
                }),
            )
                .into_response();
        }
    };
    let key = req.key.clone();
    match blocking(&state, move |idx| idx.delete(key.as_bytes())).await {
        Ok(existed) => Json(DeleteResponse {
            key: req.key,
            existed,
        })
        .into_response(),
        Err(e) => err_response(e),
    }
}

async fn stats(State(state): State<AppState>) -> Response {
    match blocking(&state, |idx| idx.stats()).await {
        Ok(s) => Json(s).into_response(),
        Err(e) => err_response(e),
    }
}

/// 二进制安全的 PUT：原始字节作为请求体，键走 `?key=` 查询参数。
async fn put_raw(
    State(state): State<AppState>,
    axum::extract::Query(q): axum::extract::Query<RawKey>,
    body: Bytes,
) -> Response {
    let key = q.key.clone();
    let value = body.to_vec();
    let n = value.len();
    match blocking(&state, move |idx| idx.put(key.as_bytes(), &value)).await {
        Ok(()) => Json(json!({ "ok": true, "key": q.key, "bytes": n })).into_response(),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
pub struct RawKey {
    pub key: String,
}

/// 构造路由（测试直接绑定到随机端口）。
pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/put", post(put))
        .route("/get", post(get_key))
        .route("/delete", post(delete_key))
        .route("/stats", get(stats))
        .route("/raw/put", post(put_raw))
        .with_state(state)
}

/// 在指定地址启动服务（阻塞，由 main 在 tokio 运行时内调用）。
pub async fn serve(index: Index, addr: &str) -> crate::errors::Result<()> {
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(IndexError::Io)?;
    let local = listener.local_addr().map_err(IndexError::Io)?;
    eprintln!("ehindex 监听 http://{local}");
    let state = AppState {
        index: Arc::new(Mutex::new(index)),
    };
    axum::serve(listener, app(state))
        .await
        .map_err(|e| IndexError::Io(std::io::Error::other(e)))?;
    Ok(())
}
