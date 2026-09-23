//! HTTP 层：Axum 路由与处理函数。核心逻辑见 [`engine`]。

pub mod engine;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use engine::{Engine, Fault};
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub engine: Arc<Engine>,
}

pub fn app(engine: Arc<Engine>) -> Router {
    let app_state = AppState { engine };
    Router::new()
        .route("/", get(index))
        .route("/kv/{key}", get(get_key).put(put_key).delete(delete_key))
        .route("/range", get(range_scan))
        .route("/admin/flush", get(noop_flush).post(flush))
        .route("/admin/compact", get(noop_compact).post(compact))
        .route("/admin/state", get(state))
        .with_state(app_state)
}

async fn index() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "service": "lsm-server",
        "endpoints": {
            "PUT /kv/{key}": "body {\"value\": \"...\"}：写入（自动带版本号）",
            "GET /kv/{key}": "点查；不存在或已删除返回 404",
            "DELETE /kv/{key}": "写入墓碑",
            "GET /range?start=&end=&limit=": "范围扫描，start 含、end 不含，按 key 升序",
            "POST /admin/flush": "手动把 memtable 刷成 L0 段",
            "POST /admin/compact?level=0&fault=none": "合并 level n 与 n+1；fault 可为 panic_after_write / exit_after_write",
            "GET /admin/state": "查看 memtable 与段清单",
        }
    }))
}

#[derive(Deserialize)]
struct PutBody {
    value: String,
}

async fn put_key(
    State(st): State<AppState>,
    Path(key): Path<String>,
    Json(body): Json<PutBody>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let seq = st
        .engine
        .put(key, body.value)
        .map_err(io_err)?;
    Ok(Json(serde_json::json!({ "ok": true, "seq": seq })))
}

async fn get_key(
    State(st): State<AppState>,
    Path(key): Path<String>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    match st.engine.get(&key) {
        Some(r) => Ok(Json(serde_json::to_value(r).unwrap())),
        None => Err((StatusCode::NOT_FOUND, format!("key {key:?} not found"))),
    }
}

async fn delete_key(
    State(st): State<AppState>,
    Path(key): Path<String>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let seq = st.engine.delete(key).map_err(io_err)?;
    Ok(Json(serde_json::json!({ "ok": true, "seq": seq, "tombstone": true })))
}

#[derive(Deserialize)]
struct RangeParams {
    start: Option<String>,
    end: Option<String>,
    limit: Option<usize>,
}

async fn range_scan(
    State(st): State<AppState>,
    Query(q): Query<RangeParams>,
) -> Json<serde_json::Value> {
    let rows = st
        .engine
        .range(q.start.as_deref(), q.end.as_deref(), q.limit);
    Json(serde_json::json!({
        "start_inclusive": q.start,
        "end_exclusive": q.end,
        "count": rows.len(),
        "items": rows,
    }))
}

async fn flush(State(st): State<AppState>) -> Result<impl IntoResponse, (StatusCode, String)> {
    let id = st.engine.flush().map_err(io_err)?;
    Ok(Json(serde_json::json!({ "ok": true, "new_segment_id": id })))
}

// GET 仅为提示：压缩是有副作用的写操作，请用 POST。
async fn noop_flush() -> impl IntoResponse {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        "use POST /admin/flush".to_string(),
    )
}

#[derive(Deserialize)]
struct CompactParams {
    level: Option<usize>,
    fault: Option<String>,
}

async fn compact(
    State(st): State<AppState>,
    Query(q): Query<CompactParams>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let level = q.level.unwrap_or(0);
    let fault = match q.fault.as_deref().unwrap_or("none") {
        "none" | "" => Fault::None,
        "panic_after_write" => Fault::PanicAfterSegWrite,
        "exit_after_write" => Fault::ExitAfterSegWrite,
        other => {
            return Err((
                StatusCode::BAD_REQUEST,
                format!("unknown fault mode {other:?}"),
            ))
        }
    };
    let report = st.engine.compact(level, fault).map_err(io_err)?;
    Ok(Json(serde_json::to_value(report).unwrap()))
}

async fn noop_compact() -> impl IntoResponse {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        "use POST /admin/compact?level=0".to_string(),
    )
}

async fn state(State(st): State<AppState>) -> Json<serde_json::Value> {
    let s = st.engine.state();
    let mut v = serde_json::to_value(&s).unwrap();
    if let serde_json::Value::Object(ref mut m) = v {
        let levels: HashMap<usize, usize> = {
            let mut h = HashMap::new();
            for seg in &s.segments {
                *h.entry(seg.level).or_default() += 1;
            }
            h
        };
        m.insert(
            "segment_count_by_level".into(),
            serde_json::to_value(levels).unwrap(),
        );
    }
    Json(v)
}

fn io_err(e: std::io::Error) -> (StatusCode, String) {
    (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
}
