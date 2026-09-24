//! HTTP 接口层(Axum)。

use crate::model::*;
use crate::store::{Store, StoreError};
use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

/// 统一错误响应体:{"error":{"code","message"}}。
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: ErrorDetail,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetail {
    pub code: &'static str,
    pub message: String,
}

impl IntoResponse for StoreError {
    fn into_response(self) -> Response {
        let (status, code) = match &self {
            StoreError::Invalid(..) => (StatusCode::BAD_REQUEST, self.code()),
            StoreError::NotFound(..) => (StatusCode::NOT_FOUND, self.code()),
            StoreError::Exists(..) => (StatusCode::CONFLICT, self.code()),
            StoreError::Conflict(code, _) => {
                let status = match *code {
                    "DIGEST_MISMATCH" | "IMMUTABLE_DIGEST" | "DIGEST_COLLISION" => {
                        StatusCode::CONFLICT
                    }
                    "APPROVAL_REQUIRED" | "PROOF_REQUIRED" | "ALREADY_AT_FINAL_STAGE"
                    | "INVALID_ROLLBACK_TARGET" => StatusCode::CONFLICT,
                    _ => StatusCode::CONFLICT,
                };
                (status, *code)
            }
        };
        let body = ErrorBody {
            error: ErrorDetail {
                code,
                message: self.to_string(),
            },
        };
        (status, Json(body)).into_response()
    }
}

fn invalid(field: &'static str, msg: impl Into<String>) -> StoreError {
    StoreError::Invalid(field, msg.into())
}

// ---------- 请求 / 响应结构 ----------

#[derive(Debug, Deserialize)]
pub struct RegisterArtifactReq {
    pub id: String,
    pub digest: String,
}

#[derive(Debug, Serialize)]
pub struct ContentUploaded {
    pub digest: String,
    pub size: usize,
    pub note: &'static str,
}

#[derive(Debug, Deserialize)]
pub struct AddProofReq {
    pub proof_id: String,
    pub digest: String,
    pub kind: String,
    pub result: String,
    #[serde(default)]
    pub detail: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AddApprovalReq {
    pub approval_id: String,
    pub digest: String,
    /// verification | release
    pub target_stage: String,
    pub approver: String,
    /// approved | rejected
    pub decision: String,
    #[serde(default)]
    pub comment: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct PromoteReq {
    /// 请求者声明的制品摘要;服务端校验其与绑定摘要一致。
    pub digest: String,
}

#[derive(Debug, Deserialize)]
pub struct RollbackReq {
    pub digest: String,
    /// dev | verification
    pub target_stage: String,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MessageResp {
    pub message: String,
}

// ---------- 处理函数 ----------

/// 上传内容(原始字节即请求体),服务端计算 SHA-256 并存入内容库。
/// 相同内容重复上传是幂等的;服务从不接受"替换"已有内容。
async fn put_content(
    State(state): State<AppState>,
    body: Bytes,
) -> Result<Json<ContentUploaded>, StoreError> {
    if body.is_empty() {
        return Err(invalid("body", "内容不能为空"));
    }
    let digest = state.store.put_blob(body.to_vec())?;
    Ok(Json(ContentUploaded {
        size: body.len(),
        digest,
        note: "content stored under its SHA-256 digest; immutable & content-addressed",
    }))
}

/// 直接按路径摘要取回内容,用于审计"回退后产物没有被重建/替换"。
async fn get_content(
    State(state): State<AppState>,
    Path(digest): Path<String>,
) -> Result<Response, StoreError> {
    let bytes = state.store.get_blob(&digest.to_ascii_lowercase())?;
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/octet-stream")],
        bytes,
    )
        .into_response())
}

async fn register_artifact(
    State(state): State<AppState>,
    Json(req): Json<RegisterArtifactReq>,
) -> Result<(StatusCode, Json<Artifact>), StoreError> {
    let (artifact, created) = state.store.register_artifact(&req.id, &req.digest)?;
    let status = if created {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(artifact)))
}

async fn list_artifacts(
    State(state): State<AppState>,
) -> Result<Json<Vec<Artifact>>, StoreError> {
    Ok(Json(state.store.list_artifacts()?))
}

async fn get_artifact(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<ArtifactView>, StoreError> {
    Ok(Json(state.store.view(&id)?))
}

async fn add_proof(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<AddProofReq>,
) -> Result<(StatusCode, Json<Proof>), StoreError> {
    let result = req
        .result
        .parse::<ProofResult>()
        .map_err(|_| invalid("result", "result 只能是 passed 或 failed"))?;
    let (proof, _) = state.store.add_proof(
        &req.proof_id,
        &id,
        &req.digest,
        &req.kind,
        result,
        req.detail,
    )?;
    Ok((StatusCode::CREATED, Json(proof)))
}

async fn add_approval(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<AddApprovalReq>,
) -> Result<(StatusCode, Json<Approval>), StoreError> {
    let target_stage = req
        .target_stage
        .parse::<Stage>()
        .map_err(|_| invalid("target_stage", "target_stage 只能是 verification 或 release"))?;
    let decision = match req.decision.as_str() {
        "approved" => Decision::Approved,
        "rejected" => Decision::Rejected,
        _ => return Err(invalid("decision", "decision 只能是 approved 或 rejected")),
    };
    let (approval, _) = state.store.add_approval(
        &req.approval_id,
        &id,
        &req.digest,
        target_stage,
        &req.approver,
        decision,
        req.comment,
    )?;
    Ok((StatusCode::CREATED, Json(approval)))
}

async fn promote(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<PromoteReq>,
) -> Result<(StatusCode, Json<Artifact>), StoreError> {
    let (artifact, changed) = state.store.promote(&id, &req.digest)?;
    // 真正发生晋级 -> 201;当前实现中重复晋级在终态返回 409,
    // 中间阶段重复晋级不会发生(晋级即离开当前阶段)。
    let status = if changed {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(artifact)))
}

async fn rollback(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(req): Json<RollbackReq>,
) -> Result<(StatusCode, Json<Artifact>), StoreError> {
    let target_stage = req
        .target_stage
        .parse::<Stage>()
        .map_err(|_| invalid("target_stage", "target_stage 必须是 dev 或 verification"))?;
    let (artifact, _changed) =
        state
            .store
            .rollback(&id, &req.digest, target_stage, req.reason)?;
    Ok((StatusCode::CREATED, Json(artifact)))
}

async fn gates(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Vec<crate::store::GateCheck>>, StoreError> {
    Ok(Json(state.store.gates(&id)?))
}

async fn health() -> Json<MessageResp> {
    Json(MessageResp {
        message: "artifact-promotion service is up".into(),
    })
}

pub fn app(store: Arc<Store>) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/health", get(health))
        .route("/artifacts/content", put(put_content))
        .route("/artifacts/content/:digest", get(get_content))
        .route("/artifacts", post(register_artifact).get(list_artifacts))
        .route("/artifacts/:id", get(get_artifact))
        .route("/artifacts/:id/proofs", post(add_proof))
        .route("/artifacts/:id/approvals", post(add_approval))
        .route("/artifacts/:id/promote", post(promote))
        .route("/artifacts/:id/rollback", post(rollback))
        .route("/artifacts/:id/gates", get(gates))
        .with_state(state)
}
