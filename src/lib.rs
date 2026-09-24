//! 远端构建缓存原型：内容寻址存储 (CAS) + 动作缓存 (AC)。
//!
//! - CAS：`PUT /cas/{sha256}` 上传输出块，服务端校验摘要后落盘；
//!   `GET /cas/{sha256}` 下载，下载前重新校验摘要，损坏即报错。
//! - AC：`PUT /ac/{action_digest}` 发布动作结果（不可变输出清单），
//!   仅当清单引用的全部对象存在且摘要核验通过才落盘；
//!   `GET /ac/{action_digest}` 查询命中，命中前同样重新核验全部对象。

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path as AxumPath, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, put};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// 输出清单中的一个条目：逻辑路径 -> CAS 对象摘要。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OutputEntry {
    pub path: String,
    pub digest: String,
    pub size: u64,
}

/// 动作结果清单（不可变）。发布后同一 action_digest 的内容不得改变。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Manifest {
    pub exit_code: i32,
    pub outputs: Vec<OutputEntry>,
}

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub missing: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub corrupt: Vec<String>,
}

impl ErrorBody {
    fn simple(msg: impl Into<String>) -> Self {
        ErrorBody {
            error: msg.into(),
            missing: vec![],
            corrupt: vec![],
        }
    }
}

fn err(status: StatusCode, body: ErrorBody) -> Response {
    (status, Json(body)).into_response()
}

#[derive(Clone)]
pub struct AppState {
    data_dir: Arc<PathBuf>,
}

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

impl AppState {
    pub fn new(data_dir: impl Into<PathBuf>) -> std::io::Result<Self> {
        let data_dir = data_dir.into();
        std::fs::create_dir_all(data_dir.join("cas"))?;
        std::fs::create_dir_all(data_dir.join("ac"))?;
        std::fs::create_dir_all(data_dir.join("tmp"))?;
        Ok(AppState {
            data_dir: Arc::new(data_dir),
        })
    }

    fn cas_path(&self, digest: &str) -> PathBuf {
        // 两级目录，避免单目录文件过多。
        self.data_dir
            .join("cas")
            .join(&digest[..2])
            .join(digest)
    }

    fn ac_path(&self, digest: &str) -> PathBuf {
        self.data_dir.join("ac").join(format!("{digest}.json"))
    }

    fn tmp_path(&self) -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        self.data_dir
            .join("tmp")
            .join(format!("{}-{n}", std::process::id()))
    }
}

/// 校验 digest 格式：64 位小写十六进制。
fn valid_digest(d: &str) -> bool {
    d.len() == 64 && d.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// 原子写：先写临时文件，再 rename。并发写同一目标时内容相同，最后一个 rename 生效。
fn atomic_write(path: &Path, state: &AppState, data: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = state.tmp_path();
    std::fs::write(&tmp, data)?;
    // rename 失败时清理临时文件，避免 tmp 目录堆积。
    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }
    Ok(())
}

/// 读取 CAS 对象并重新核验摘要。Ok(None) 表示不存在。
fn read_verified_blob(state: &AppState, digest: &str) -> Result<Option<Vec<u8>>, Response> {
    let path = state.cas_path(digest);
    let data = match std::fs::read(&path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => {
            return Err(err(
                StatusCode::INTERNAL_SERVER_ERROR,
                ErrorBody::simple(format!("read cas object failed: {e}")),
            ))
        }
    };
    if sha256_hex(&data) != digest {
        tracing::error!(digest, "cas object corrupt on disk");
        return Err(err(
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorBody {
                error: format!("cas object {digest} failed digest verification (corrupt)"),
                missing: vec![],
                corrupt: vec![digest.to_string()],
            },
        ));
    }
    Ok(Some(data))
}

async fn put_blob(
    State(state): State<AppState>,
    AxumPath(digest): AxumPath<String>,
    body: Bytes,
) -> Response {
    if !valid_digest(&digest) {
        return err(
            StatusCode::BAD_REQUEST,
            ErrorBody::simple("digest must be 64 lowercase hex chars (sha256)"),
        );
    }
    let actual = sha256_hex(&body);
    if actual != digest {
        return err(
            StatusCode::BAD_REQUEST,
            ErrorBody::simple(format!(
                "content digest mismatch: expected {digest}, got {actual}"
            )),
        );
    }
    let path = state.cas_path(&digest);
    if path.exists() {
        // 已存在且上传内容摘要一致：幂等成功。
        return StatusCode::OK.into_response();
    }
    match atomic_write(&path, &state, &body) {
        Ok(()) => {
            tracing::info!(digest, size = body.len(), "cas object stored");
            StatusCode::CREATED.into_response()
        }
        Err(e) => err(
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorBody::simple(format!("store cas object failed: {e}")),
        ),
    }
}

async fn head_blob(
    State(state): State<AppState>,
    AxumPath(digest): AxumPath<String>,
) -> StatusCode {
    if !valid_digest(&digest) {
        return StatusCode::BAD_REQUEST;
    }
    if state.cas_path(&digest).exists() {
        StatusCode::OK
    } else {
        StatusCode::NOT_FOUND
    }
}

async fn get_blob(
    State(state): State<AppState>,
    AxumPath(digest): AxumPath<String>,
) -> Response {
    if !valid_digest(&digest) {
        return err(
            StatusCode::BAD_REQUEST,
            ErrorBody::simple("digest must be 64 lowercase hex chars (sha256)"),
        );
    }
    match read_verified_blob(&state, &digest) {
        Ok(Some(data)) => {
            let mut headers = HeaderMap::new();
            headers.insert(header::CONTENT_TYPE, "application/octet-stream".parse().unwrap());
            (StatusCode::OK, headers, data).into_response()
        }
        Ok(None) => err(
            StatusCode::NOT_FOUND,
            ErrorBody::simple(format!("cas object {digest} not found")),
        ),
        Err(resp) => resp,
    }
}

/// 核验清单引用的全部对象：返回 (missing, corrupt)。
fn verify_manifest_objects(state: &AppState, manifest: &Manifest) -> (Vec<String>, Vec<String>) {
    let mut missing = Vec::new();
    let mut corrupt = Vec::new();
    for out in &manifest.outputs {
        if !valid_digest(&out.digest) {
            corrupt.push(out.digest.clone());
            continue;
        }
        match read_verified_blob(state, &out.digest) {
            Ok(Some(_)) => {}
            Ok(None) => missing.push(out.digest.clone()),
            Err(_) => corrupt.push(out.digest.clone()),
        }
    }
    (missing, corrupt)
}

async fn put_action(
    State(state): State<AppState>,
    AxumPath(action_digest): AxumPath<String>,
    Json(manifest): Json<Manifest>,
) -> Response {
    if !valid_digest(&action_digest) {
        return err(
            StatusCode::BAD_REQUEST,
            ErrorBody::simple("action digest must be 64 lowercase hex chars (sha256)"),
        );
    }
    // 只有全部对象核验完成才发布。
    let (missing, corrupt) = verify_manifest_objects(&state, &manifest);
    if !missing.is_empty() || !corrupt.is_empty() {
        return err(
            StatusCode::UNPROCESSABLE_ENTITY,
            ErrorBody {
                error: "refusing to publish action result: unverified objects".to_string(),
                missing,
                corrupt,
            },
        );
    }
    let path = state.ac_path(&action_digest);
    if path.exists() {
        // 已发布：内容必须一致（不可变），一致则幂等成功，否则冲突。
        match std::fs::read(&path) {
            Ok(existing) => match serde_json::from_slice::<Manifest>(&existing) {
                Ok(existing_m) if existing_m == manifest => return StatusCode::OK.into_response(),
                _ => {
                    return err(
                        StatusCode::CONFLICT,
                        ErrorBody::simple(
                            "action result already published with different content (immutable)",
                        ),
                    )
                }
            },
            Err(e) => {
                return err(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    ErrorBody::simple(format!("read existing action result failed: {e}")),
                )
            }
        }
    }
    let data = match serde_json::to_vec(&manifest) {
        Ok(d) => d,
        Err(e) => {
            return err(
                StatusCode::INTERNAL_SERVER_ERROR,
                ErrorBody::simple(format!("serialize manifest failed: {e}")),
            )
        }
    };
    match atomic_write(&path, &state, &data) {
        Ok(()) => {
            tracing::info!(action_digest, "action result published");
            StatusCode::CREATED.into_response()
        }
        Err(e) => err(
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorBody::simple(format!("publish action result failed: {e}")),
        ),
    }
}

async fn get_action(
    State(state): State<AppState>,
    AxumPath(action_digest): AxumPath<String>,
) -> Response {
    if !valid_digest(&action_digest) {
        return err(
            StatusCode::BAD_REQUEST,
            ErrorBody::simple("action digest must be 64 lowercase hex chars (sha256)"),
        );
    }
    let path = state.ac_path(&action_digest);
    let data = match std::fs::read(&path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return err(
                StatusCode::NOT_FOUND,
                ErrorBody::simple(format!("action {action_digest} not in cache")),
            )
        }
        Err(e) => {
            return err(
                StatusCode::INTERNAL_SERVER_ERROR,
                ErrorBody::simple(format!("read action result failed: {e}")),
            )
        }
    };
    let manifest: Manifest = match serde_json::from_slice(&data) {
        Ok(m) => m,
        Err(e) => {
            return err(
                StatusCode::INTERNAL_SERVER_ERROR,
                ErrorBody::simple(format!("cached action result is corrupt: {e}")),
            )
        }
    };
    // 命中前重新核验全部对象：任何对象缺失/损坏都不得返回伪成功。
    let (missing, corrupt) = verify_manifest_objects(&state, &manifest);
    if !missing.is_empty() || !corrupt.is_empty() {
        return err(
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorBody {
                error: "cached action result references unverified objects".to_string(),
                missing,
                corrupt,
            },
        );
    }
    (StatusCode::OK, Json(manifest)).into_response()
}

pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/cas/:digest", put(put_blob).get(get_blob).head(head_blob))
        .route("/ac/:digest", put(put_action).get(get_action))
        .route("/healthz", get(|| async { "ok" }))
        .with_state(state)
}
