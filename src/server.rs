//! Axum HTTP 服务：策略查询、证明验证、夹具场景一键复现。

use crate::policy::Verifier;
use crate::types::{VerificationReport, VerifyRequest};
use crate::crypto::sha256;
use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub verifier: Arc<Verifier>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/policy", get(get_policy))
        .route("/verify", post(verify))
        .route("/scenarios", get(list_scenarios))
        .route("/scenarios/{name}", post(run_scenario))
        .with_state(state)
}

pub async fn serve(verifier: Verifier, addr: SocketAddr) -> anyhow::Result<()> {
    let app = router(AppState {
        verifier: Arc::new(verifier),
    });
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("build-provenance-verify listening on http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({"status": "ok", "service": "build-provenance-verify"}))
}

async fn get_policy(State(state): State<AppState>) -> Json<serde_json::Value> {
    let p = &state.verifier.policy;
    let mut trusted: Vec<&str> = p.trusted_builders.iter().map(String::as_str).collect();
    trusted.sort();
    let mut repos: Vec<&str> = p.allowed_source_repositories.iter().map(String::as_str).collect();
    repos.sort();
    Json(json!({
        "trusted_builders": trusted,
        "allowed_material_prefixes": p.allowed_material_prefixes,
        "allowed_source_repositories": repos,
        "registered_keyids": state.verifier.keys.registered_keyids(),
        "local_materials": state.verifier.materials.uris(),
    }))
}

fn error_response(status: StatusCode, code: &str, message: String) -> Response {
    (status, Json(json!({"error": code, "message": message}))).into_response()
}

/// 把请求中的 `artifact_path` 解析为夹具 artifacts 目录下文件的实际摘要。
fn resolve_artifact_path(
    state: &AppState,
    req: &mut VerifyRequest,
) -> Result<(), (StatusCode, String, String)> {
    let Some(rel) = req.artifact_path.clone() else {
        return Ok(());
    };
    let artifacts_dir = state.verifier.fixtures_dir.join("artifacts");
    let candidate = artifacts_dir.join(&rel);
    let canonical = candidate
        .canonicalize()
        .map_err(|e| {
            (
                StatusCode::BAD_REQUEST,
                "ARTIFACT_NOT_FOUND".to_string(),
                format!("无法定位 artifact_path `{rel}`: {e}"),
            )
        })?;
    let root = artifacts_dir
        .canonicalize()
        .map_err(|e| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "FIXTURE_ERROR".to_string(),
                e.to_string(),
            )
        })?;
    if !canonical.starts_with(&root) {
        return Err((
            StatusCode::BAD_REQUEST,
            "BAD_PATH".to_string(),
            "artifact_path 越出 fixtures/artifacts 目录".to_string(),
        ));
    }
    let bytes = std::fs::read(&canonical).map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            "ARTIFACT_READ_FAILED".to_string(),
            e.to_string(),
        )
    })?;
    let digest = hex::encode(sha256(&bytes));
    req.actual_output_digest = Some(digest);
    req.artifact_path = None;
    Ok(())
}

async fn verify(
    State(state): State<AppState>,
    Json(mut req): Json<VerifyRequest>,
) -> Response {
    if let Err((status, code, msg)) = resolve_artifact_path(&state, &mut req) {
        return error_response(status, &code, msg);
    }

    let envelope = match (&req.envelope, &req.proof_path) {
        (Some(e), _) => e.clone(),
        (None, Some(rel)) => match state.verifier.load_proof_envelope(rel) {
            Ok(e) => e,
            Err(e) => {
                return error_response(
                    StatusCode::BAD_REQUEST,
                    "PROOF_LOAD_FAILED",
                    format!("{e:#}"),
                )
            }
        },
        (None, None) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                "MISSING_PROOF",
                "必须提供 envelope 或 proof_path".to_string(),
            )
        }
    };

    let report = state.verifier.verify(&envelope, &req);
    let status = if report.accepted {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    (status, Json(report)).into_response()
}

async fn list_scenarios(State(state): State<AppState>) -> Response {
    let dir = state.verifier.fixtures_dir.join("requests");
    let mut scenarios = Vec::new();
    match std::fs::read_dir(&dir) {
        Ok(entries) => {
            for entry in entries.flatten() {
                if let Some(name) = entry.file_name().to_str() {
                    if let Some(stem) = name.strip_suffix(".json") {
                        scenarios.push(stem.to_string());
                    }
                }
            }
        }
        Err(e) => {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "FIXTURE_ERROR",
                format!("读取 requests 夹具失败: {e}"),
            );
        }
    }
    scenarios.sort();
    Json(json!({"scenarios": scenarios})).into_response()
}

async fn run_scenario(
    State(state): State<AppState>,
    AxumPath(name): AxumPath<String>,
) -> Response {
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return error_response(
            StatusCode::BAD_REQUEST,
            "BAD_SCENARIO_NAME",
            "场景名只能包含字母数字、下划线、连字符".to_string(),
        );
    }
    let path: PathBuf = state
        .verifier
        .fixtures_dir
        .join("requests")
        .join(format!("{name}.json"));
    let raw = match std::fs::read(&path) {
        Ok(b) => b,
        Err(_) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "SCENARIO_NOT_FOUND",
                format!("场景 `{name}` 不存在"),
            )
        }
    };
    let mut req: VerifyRequest = match serde_json::from_slice(&raw) {
        Ok(r) => r,
        Err(e) => {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "FIXTURE_ERROR",
                format!("场景夹具解析失败: {e}"),
            )
        }
    };
    if let Err((status, code, msg)) = resolve_artifact_path(&state, &mut req) {
        return error_response(status, &code, msg);
    }
    let envelope = match state.verifier.load_proof_envelope(
        req.proof_path
            .as_deref()
            .expect("scenario fixture must set proof_path"),
    ) {
        Ok(e) => e,
        Err(e) => {
            return error_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                "PROOF_LOAD_FAILED",
                format!("{e:#}"),
            )
        }
    };
    let report: VerificationReport = state.verifier.verify(&envelope, &req);
    let status = if report.accepted {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    (status, Json(json!({"scenario": name, "report": report}))).into_response()
}
