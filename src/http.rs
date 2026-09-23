//! Axum HTTP 接口。
//!
//! | 方法 | 路径 | 说明 |
//! |---|---|---|
//! | `POST` | `/insert` | JSON `{"key": i64, "value": u64}`，插入或覆盖 |
//! | `POST` | `/delete` | JSON `{"key": i64}`，删除 |
//! | `GET`  | `/get?key=i64` | 点查 |
//! | `GET`  | `/range?lo=i64&hi=i64` | 闭区间有序范围读 |
//! | `GET`  | `/stats` | 全树校验 + 统计（树高、页数、占用率、叶链） |
//! | `GET`  | `/healthz` | 存活探针 |

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get as route_get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};

use crate::bptree::BPTree;
use crate::pager::Pager;

/// 共享树状态。`P` 一般为 [`crate::pager::FilePager`]，测试时可用内存实现。
pub type SharedTree<P> = Arc<Mutex<BPTree<P>>>;

pub fn router<P: Pager + Send + 'static>(tree: SharedTree<P>) -> Router {
    Router::new()
        .route("/insert", post(insert::<P>))
        .route("/delete", post(delete::<P>))
        .route("/get", route_get(get::<P>))
        .route("/range", route_get(range::<P>))
        .route("/stats", route_get(stats::<P>))
        .route("/healthz", route_get(|| async { "ok\n" }))
        .with_state(tree)
}

#[derive(Debug, Deserialize)]
pub struct InsertReq {
    pub key: i64,
    pub value: u64,
}

#[derive(Debug, Serialize)]
pub struct InsertResp {
    pub ok: bool,
    /// true=新插入；false=键已存在、值被覆盖。
    pub inserted: bool,
}

#[derive(Debug, Deserialize)]
pub struct DeleteReq {
    pub key: i64,
}

#[derive(Debug, Serialize)]
pub struct DeleteResp {
    pub ok: bool,
    pub found: bool,
}

#[derive(Debug, Deserialize)]
pub struct KeyQuery {
    pub key: i64,
}

#[derive(Debug, Serialize)]
pub struct GetResp {
    pub key: i64,
    pub found: bool,
    pub value: Option<u64>,
}

#[derive(Debug, Deserialize)]
pub struct RangeQuery {
    pub lo: i64,
    pub hi: i64,
}

#[derive(Debug, Serialize)]
pub struct KV {
    pub key: i64,
    pub value: u64,
}

#[derive(Debug, Serialize)]
pub struct RangeResp {
    pub lo: i64,
    pub hi: i64,
    pub count: usize,
    pub items: Vec<KV>,
}

#[derive(Debug, Serialize)]
pub struct StatsResp {
    pub ok: bool,
    pub page_size: usize,
    pub leaf_capacity: usize,
    pub height: usize,
    pub internal_pages: usize,
    pub leaf_pages: usize,
    pub item_count: usize,
    pub min_occupancy: f64,
    pub max_occupancy: f64,
    pub occupancies: Vec<f64>,
}

/// 统一错误响应：树校验失败等返回 500，参数错误返回 400。
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
    fn into_response(self) -> axum::response::Response {
        (
            self.status,
            Json(serde_json::json!({ "ok": false, "error": self.message })),
        )
            .into_response()
    }
}

async fn insert<P: Pager + Send>(
    State(tree): State<SharedTree<P>>,
    Json(req): Json<InsertReq>,
) -> Result<Json<InsertResp>, ApiError> {
    let mut t = tree.lock().unwrap();
    let inserted = t.insert(req.key, req.value);
    t.sync().map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(Json(InsertResp { ok: true, inserted }))
}

async fn delete<P: Pager + Send>(
    State(tree): State<SharedTree<P>>,
    Json(req): Json<DeleteReq>,
) -> Result<Json<DeleteResp>, ApiError> {
    let mut t = tree.lock().unwrap();
    let found = t.delete(req.key);
    t.sync().map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(Json(DeleteResp { ok: true, found }))
}

async fn get<P: Pager + Send>(
    State(tree): State<SharedTree<P>>,
    Query(q): Query<KeyQuery>,
) -> Result<Json<GetResp>, ApiError> {
    let t = tree.lock().unwrap();
    let value = t.get(q.key);
    Ok(Json(GetResp {
        key: q.key,
        found: value.is_some(),
        value,
    }))
}

async fn range<P: Pager + Send>(
    State(tree): State<SharedTree<P>>,
    Query(q): Query<RangeQuery>,
) -> Result<Json<RangeResp>, ApiError> {
    if q.lo > q.hi {
        return Err(ApiError::bad_request("lo 必须 <= hi"));
    }
    let t = tree.lock().unwrap();
    let items = t
        .range(q.lo, q.hi)
        .into_iter()
        .map(|(key, value)| KV { key, value })
        .collect::<Vec<_>>();
    let count = items.len();
    Ok(Json(RangeResp {
        lo: q.lo,
        hi: q.hi,
        count,
        items,
    }))
}

async fn stats<P: Pager + Send>(
    State(tree): State<SharedTree<P>>,
) -> Result<Json<StatsResp>, ApiError> {
    let t = tree.lock().unwrap();
    let page_size = t.page_size();
    let leaf_capacity = t.leaf_capacity();
    let s = t
        .verify()
        .map_err(|e| ApiError::internal(format!("树校验失败：{e}")))?;
    let min_occupancy = s.occupancies.iter().cloned().fold(1.0_f64, f64::min);
    let max_occupancy = s.occupancies.iter().cloned().fold(0.0_f64, f64::max);
    Ok(Json(StatsResp {
        ok: true,
        page_size,
        leaf_capacity,
        height: s.height,
        internal_pages: s.internal_pages,
        leaf_pages: s.leaf_pages,
        item_count: s.item_count,
        min_occupancy,
        max_occupancy,
        occupancies: s.occupancies,
    }))
}
