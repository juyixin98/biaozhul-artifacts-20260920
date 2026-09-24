//! Axum HTTP 接口层。
//!
//! | 方法   | 路径                       | 说明                       |
//! |--------|----------------------------|----------------------------|
//! | GET    | `/healthz`                 | 健康检查                   |
//! | GET    | `/keys/{key}`              | 点查                       |
//! | PUT    | `/keys/{key}`              | 插入/覆盖，body `{"value": i64}` |
//! | DELETE | `/keys/{key}`              | 删除                       |
//! | GET    | `/range?start=&end=&limit=`| 闭区间有序扫描（均可选）   |
//! | GET    | `/stats`                   | 元数据与占用率统计         |
//! | POST   | `/verify`                  | 全量结构校验               |

use std::sync::Mutex;

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::tree::{BpTree, DelOutcome, PutOutcome};

/// 共享状态：整棵树用一把互斥锁串行化（纯后端演示足够）。
pub struct AppState {
    pub tree: Mutex<BpTree>,
}

impl AppState {
    pub fn new(tree: BpTree) -> Self {
        AppState {
            tree: Mutex::new(tree),
        }
    }
}

/// 构造路由，可挂载到任意 axum 测试/服务端。
pub fn build_router(state: std::sync::Arc<AppState>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/keys/{key}", get(get_key).put(put_key).delete(delete_key))
        .route("/range", get(range))
        .route("/stats", get(stats))
        .route("/verify", post(verify))
        .with_state(state)
}

/// 统一错误类型：业务/IO 错误返回 JSON。
struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn bad_request(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            message: msg.into(),
        }
    }

    fn internal(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: msg.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(serde_json::json!({ "error": self.message })),
        )
            .into_response()
    }
}

impl From<std::io::Error> for ApiError {
    fn from(e: std::io::Error) -> Self {
        ApiError::internal(format!("磁盘 IO 错误：{e}"))
    }
}

fn parse_key(raw: &str) -> Result<i64, ApiError> {
    raw.parse::<i64>()
        .map_err(|_| ApiError::bad_request(format!("键必须是 i64 整数，收到 {raw:?}")))
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

#[derive(Serialize)]
struct GetResp {
    key: i64,
    found: bool,
    value: Option<i64>,
}

async fn get_key(
    State(state): State<std::sync::Arc<AppState>>,
    Path(raw): Path<String>,
) -> Result<(StatusCode, Json<GetResp>), ApiError> {
    let key = parse_key(&raw)?;
    let mut tree = state.tree.lock().unwrap();
    let value = tree.get(key)?;
    Ok((
        StatusCode::OK,
        Json(GetResp {
            key,
            found: value.is_some(),
            value,
        }),
    ))
}

#[derive(Deserialize)]
struct PutBody {
    value: i64,
}

#[derive(Serialize)]
struct PutResp {
    key: i64,
    value: i64,
    inserted: bool,
    replaced: Option<i64>,
}

async fn put_key(
    State(state): State<std::sync::Arc<AppState>>,
    Path(raw): Path<String>,
    Json(body): Json<PutBody>,
) -> Result<(StatusCode, Json<PutResp>), ApiError> {
    let key = parse_key(&raw)?;
    let mut tree = state.tree.lock().unwrap();
    let outcome = tree.put(key, body.value)?;
    let (status, replaced) = match outcome {
        PutOutcome::Inserted => (StatusCode::CREATED, None),
        PutOutcome::Replaced(old) => (StatusCode::OK, Some(old)),
    };
    Ok((
        status,
        Json(PutResp {
            key,
            value: body.value,
            inserted: replaced.is_none(),
            replaced,
        }),
    ))
}

#[derive(Serialize)]
struct DeleteResp {
    key: i64,
    deleted: bool,
}

async fn delete_key(
    State(state): State<std::sync::Arc<AppState>>,
    Path(raw): Path<String>,
) -> Result<Json<DeleteResp>, ApiError> {
    let key = parse_key(&raw)?;
    let mut tree = state.tree.lock().unwrap();
    let outcome = tree.delete(key)?;
    Ok(Json(DeleteResp {
        key,
        deleted: matches!(outcome, DelOutcome::Deleted),
    }))
}

#[derive(Deserialize)]
struct RangeQuery {
    start: Option<String>,
    end: Option<String>,
    limit: Option<String>,
}

#[derive(Serialize)]
struct Pair {
    key: i64,
    value: i64,
}

#[derive(Serialize)]
struct RangeResp {
    start: Option<i64>,
    end: Option<i64>,
    pairs: Vec<Pair>,
    truncated: bool,
}

async fn range(
    State(state): State<std::sync::Arc<AppState>>,
    Query(q): Query<RangeQuery>,
) -> Result<Json<RangeResp>, ApiError> {
    let parse_bound = |name: &str, raw: Option<String>| -> Result<Option<i64>, ApiError> {
        match raw {
            None => Ok(None),
            Some(s) => s
                .parse::<i64>()
                .map(Some)
                .map_err(|_| ApiError::bad_request(format!("{name} 必须是 i64 整数"))),
        }
    };
    let start = parse_bound("start", q.start)?;
    let end = parse_bound("end", q.end)?;
    let limit = match q.limit {
        None => None,
        Some(s) => Some(s.parse::<usize>().map_err(|_| {
            ApiError::bad_request("limit 必须是非负整数")
        })?),
    };

    let mut tree = state.tree.lock().unwrap();
    let pairs = tree.range(start, end)?;
    let truncated = limit.is_some_and(|lim| pairs.len() > lim);
    let mut pairs = pairs;
    if let Some(lim) = limit {
        pairs.truncate(lim);
    }
    Ok(Json(RangeResp {
        start,
        end,
        pairs: pairs
            .into_iter()
            .map(|(key, value)| Pair { key, value })
            .collect(),
        truncated,
    }))
}

#[derive(Serialize)]
struct StatsResp {
    page_size: u16,
    root_page: u32,
    leftmost_page: u32,
    free_list_head: u32,
    key_count: u64,
    leaf_capacity: usize,
    internal_capacity: usize,
    leaf_min_fill: usize,
    internal_min_separators: usize,
}

async fn stats(
    State(state): State<std::sync::Arc<AppState>>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let tree = state.tree.lock().unwrap();
    let limits = tree.limits();
    let meta = tree.meta_snapshot();
    Ok(Json(serde_json::json!(StatsResp {
        page_size: meta.page_size,
        root_page: meta.root,
        leftmost_page: meta.leftmost,
        free_list_head: meta.free_head,
        key_count: meta.key_count,
        leaf_capacity: limits.leaf_max,
        internal_capacity: limits.internal_max,
        leaf_min_fill: limits.leaf_min(),
        internal_min_separators: limits.internal_min_seps(),
    })))
}

async fn verify(
    State(state): State<std::sync::Arc<AppState>>,
) -> Result<Response, ApiError> {
    let mut tree = state.tree.lock().unwrap();
    let report = tree.verify()?;
    if report.ok {
        Ok(Json(report).into_response())
    } else {
        Ok((StatusCode::INTERNAL_SERVER_ERROR, Json(report)).into_response())
    }
}
