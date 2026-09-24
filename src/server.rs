//! HTTP 服务层（axum 路由与处理器），与打包核心分离，便于测试复用。

use crate::pack::{pack, PackError};
use axum::{
    body::Body,
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone, Default)]
pub struct AppState {
    _private: Arc<()>,
}

#[derive(Deserialize)]
pub struct PackRequest {
    /// 要打包的目录（服务端文件系统路径）
    root: String,
    /// 可选：仅打包这些相对路径；缺省为扫描整个目录
    paths: Option<Vec<String>>,
}

#[derive(serde::Serialize)]
struct ErrorBody {
    error: String,
}

fn error_response(err: &PackError) -> Response {
    let status = match err {
        PackError::RootNotFound(_) => StatusCode::NOT_FOUND,
        PackError::Conflict(_) => StatusCode::CONFLICT, // 冲突规范路径：明确拒绝
        PackError::RootNotDir(_)
        | PackError::Normalize { .. }
        | PackError::UnsupportedType(_) => StatusCode::BAD_REQUEST,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    };
    (
        status,
        Json(ErrorBody {
            error: err.to_string(),
        }),
    )
        .into_response()
}

async fn health() -> &'static str {
    "ok"
}

async fn pack_handler(
    State(_state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<PackRequest>,
) -> Response {
    let root = PathBuf::from(&req.root);
    let result = match pack(&root, req.paths.as_deref()) {
        Ok(r) => r,
        Err(e) => return error_response(&e),
    };

    let wants_tar = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.contains("application/x-tar"))
        .unwrap_or(false);

    if wants_tar {
        let mut resp = Response::new(Body::from(result.tar_bytes));
        resp.headers_mut()
            .insert(header::CONTENT_TYPE, "application/x-tar".parse().unwrap());
        resp.headers_mut().insert(
            "X-Archive-Sha256",
            result.manifest.archive.sha256.parse().unwrap(),
        );
        resp.headers_mut().insert(
            "X-Archive-Size",
            result.manifest.archive.size_bytes.to_string().parse().unwrap(),
        );
        resp
    } else {
        Json(result.manifest).into_response()
    }
}

pub fn build_router() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/pack", post(pack_handler))
        .with_state(AppState::default())
}
