//! HTTP layer (Axum).
//!
//! Endpoints:
//!
//! | Method | Path      | Body                                                        |
//! |--------|-----------|-------------------------------------------------------------|
//! | GET    | `/health` | —                                                           |
//! | POST   | `/pack`   | `{source, output_dir, artifact_name?, mtime_epoch?, overwrite?}` |
//! | POST   | `/verify` | `{tar_path, manifest_path?, digest_path?, source?}`         |
//!
//! Error responses share the shape `{"error": {"code": ..., "message": ...}}`
//! with status 400 (invalid input), 409 (determinism-policy conflict) or 500
//! (IO/internal).

use std::net::SocketAddr;

use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::pack::{pack, PackOptions};
use crate::verify::{verify, VerifyOptions};
use crate::Error;

#[derive(Clone)]
struct AppState;

/// Builds the application router.
pub fn app() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/pack", post(pack_handler))
        .route("/verify", post(verify_handler))
        // Bodies are tiny path payloads; reject anything larger.
        .layer(DefaultBodyLimit::max(256 * 1024))
        .with_state(AppState)
}

/// Binds and serves until shutdown.
pub async fn serve(addr: SocketAddr) -> Result<(), std::io::Error> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app())
        .with_graceful_shutdown(shutdown_signal())
        .await
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "repro-pack" }))
}

#[derive(Debug, Deserialize)]
struct PackRequest {
    source: String,
    output_dir: String,
    artifact_name: Option<String>,
    mtime_epoch: Option<i64>,
    overwrite: Option<bool>,
}

#[derive(Debug, Serialize)]
struct PackResponse {
    tar_path: String,
    manifest_path: String,
    digest_path: String,
    sha256: String,
    archive_size: u64,
    entry_count: usize,
}

async fn pack_handler(
    State(..): State<AppState>,
    body: Bytes,
) -> Result<Json<PackResponse>, ApiError> {
    let req: PackRequest = serde_json::from_slice(&body).map_err(|e| {
        bad_request(
            "invalid_json",
            format!("request body is not valid JSON: {e}"),
        )
    })?;
    if req.source.trim().is_empty() {
        return Err(bad_request("missing_field", "source is required"));
    }
    if req.output_dir.trim().is_empty() {
        return Err(bad_request("missing_field", "output_dir is required"));
    }
    let name = req.artifact_name.unwrap_or_else(|| "artifact".to_string());
    let mut opts = PackOptions::new(req.source, req.output_dir, name);
    opts.mtime_epoch = req.mtime_epoch.unwrap_or(crate::pack::DEFAULT_MTIME_EPOCH);
    opts.overwrite = req.overwrite.unwrap_or(false);

    // All filesystem work is blocking — run on the blocking pool.
    let outcome = tokio::task::spawn_blocking(move || pack(&opts))
        .await
        .map_err(|e| internal(format!("pack task panicked: {e}")))??;

    Ok(Json(PackResponse {
        tar_path: outcome.tar_path.display().to_string(),
        manifest_path: outcome.manifest_path.display().to_string(),
        digest_path: outcome.digest_path.display().to_string(),
        sha256: outcome.sha256,
        archive_size: outcome.archive_size,
        entry_count: outcome.entry_count,
    }))
}

#[derive(Debug, Deserialize)]
struct VerifyRequest {
    tar_path: String,
    manifest_path: Option<String>,
    digest_path: Option<String>,
    source: Option<String>,
}

#[derive(Debug, Serialize)]
struct VerifyResponse {
    ok: bool,
    sha256: String,
    archive_size: u64,
    entry_count: usize,
    digest_matches: Option<bool>,
    manifest_matches: bool,
    rebuild_matches: Option<bool>,
    problems: Vec<String>,
}

async fn verify_handler(
    State(..): State<AppState>,
    body: Bytes,
) -> Result<(StatusCode, Json<VerifyResponse>), ApiError> {
    let req: VerifyRequest = serde_json::from_slice(&body).map_err(|e| {
        bad_request(
            "invalid_json",
            format!("request body is not valid JSON: {e}"),
        )
    })?;
    if req.tar_path.trim().is_empty() {
        return Err(bad_request("missing_field", "tar_path is required"));
    }
    let mut opts = VerifyOptions::new(req.tar_path);
    opts.manifest_path = req.manifest_path.map(Into::into);
    opts.digest_path = req.digest_path.map(Into::into);
    opts.source = req.source.map(Into::into);

    let outcome = tokio::task::spawn_blocking(move || verify(&opts))
        .await
        .map_err(|e| internal(format!("verify task panicked: {e}")))??;

    let status = if outcome.ok {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    Ok((
        status,
        Json(VerifyResponse {
            ok: outcome.ok,
            sha256: outcome.sha256,
            archive_size: outcome.archive_size,
            entry_count: outcome.entry_count,
            digest_matches: outcome.digest_matches,
            manifest_matches: outcome.manifest_matches,
            rebuild_matches: outcome.rebuild_matches,
            problems: outcome.problems,
        }),
    ))
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

struct ApiError {
    status: StatusCode,
    code: String,
    message: String,
}

fn bad_request(code: &str, message: impl Into<String>) -> ApiError {
    ApiError {
        status: StatusCode::BAD_REQUEST,
        code: code.to_string(),
        message: message.into(),
    }
}

fn internal(message: String) -> ApiError {
    ApiError {
        status: StatusCode::INTERNAL_SERVER_ERROR,
        code: "internal".to_string(),
        message,
    }
}

impl From<Error> for ApiError {
    fn from(e: Error) -> Self {
        match e {
            Error::Invalid { code, message } => ApiError {
                status: StatusCode::BAD_REQUEST,
                code: code.to_string(),
                message,
            },
            Error::Conflict(message) => ApiError {
                status: StatusCode::CONFLICT,
                code: "canonical_path_conflict".to_string(),
                message,
            },
            Error::Io(err) => internal(err.to_string()),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(serde_json::json!({
            "error": { "code": self.code, "message": self.message }
        }));
        (
            self.status,
            [(header::CONTENT_TYPE, "application/json")],
            body,
        )
            .into_response()
    }
}
