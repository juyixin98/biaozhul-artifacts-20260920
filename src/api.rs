//! Axum HTTP API.
//!
//! Routes
//! ------
//! ```text
//! GET  /healthz
//! GET  /v1/info                      service + current version
//! GET  /v1/versions                  list every published version
//! GET  /v1/roots/:version            one version's root metadata
//! POST /v1/batches                   apply a write batch -> new root
//! GET  /v1/keys/:key_hex             current value of a key
//! GET  /v1/proofs/key/:key_hex?version=N
//! POST /v1/verify                    verify an arbitrary proof against a root
//! ```
//!
//! All binary data (keys, values, hashes) is lower-case hex in JSON.

use axum::{
    body::Bytes,
    extract::{FromRequest, Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use axum::extract::Request as AxumRequest;
use serde::{Deserialize, Serialize};
use serde_json::json;

/// JSON body wrapper that turns extraction failures (malformed JSON, wrong
/// shape) into our own 400 `bad_request` JSON error instead of Axum's plain
/// 422 text, keeping the error envelope uniform.
#[derive(Debug)]
struct AppJson<T>(T);

impl<T, S> FromRequest<S> for AppJson<T>
where
    T: for<'de> Deserialize<'de>,
    S: Send + Sync,
{
    type Rejection = AppError;

    async fn from_request(req: AxumRequest, state: &S) -> std::result::Result<Self, Self::Rejection> {
        let bytes = Bytes::from_request(req, state)
            .await
            .map_err(|e| AppError(Error::bad(format!("cannot read request body: {e}"))))?;
        let value: T = serde_json::from_slice(&bytes)
            .map_err(|e| AppError(Error::bad(format!("invalid JSON request body: {e}"))))?;
        Ok(AppJson(value))
    }
}

#[derive(Debug)]
struct AppError(Error);

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        self.0.into_response()
    }
}

use crate::encoding::{Hex32, HexBytes};
use crate::error::Error;
use crate::proof::{
    verify_inclusion, verify_non_existence, InclusionProof, NonExistenceProof, ProofResponse,
};
use crate::service::Service;
use crate::store::WriteOp;

/// Build the application router.
pub fn router(service: Service) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/info", get(info))
        .route("/v1/versions", get(list_versions))
        .route("/v1/roots/{version}", get(root_of))
        .route("/v1/batches", post(apply_batch))
        .route("/v1/keys/{key_hex}", get(get_key))
        .route("/v1/proofs/key/{key_hex}", get(get_proof))
        .route("/v1/verify", post(verify))
        .with_state(service)
}

// ---------------------------------------------------------------------------
// DTOs
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum BatchItemDto {
    /// Set a key. `value` hex may be "" to store an empty value.
    Put { key: HexBytes, value: HexBytes },
    /// Delete a key.
    Delete { key: HexBytes },
}

#[derive(Debug, Deserialize)]
struct BatchRequest {
    /// Ordered operations; last op for a duplicate key wins.
    ops: Vec<BatchItemDto>,
}

#[derive(Debug, Serialize)]
struct RootDto {
    version: u64,
    root: Hex32,
    leaf_count: u64,
    created_at_unix_ns: u64,
}

#[derive(Debug, Deserialize)]
struct VersionQuery {
    version: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum VerifyRequest {
    Inclusion { root: Hex32, proof: Box<InclusionProof> },
    NonExistence { root: Hex32, proof: Box<NonExistenceProof> },
    /// Full `/v1/proofs/...` response can be fed straight back in.
    Response { root: Hex32, response: Box<ProofResponse> },
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let (status, code) = match &self {
            Error::UnknownVersion(_) => (StatusCode::NOT_FOUND, "unknown_version"),
            Error::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
            Error::Storage(_) | Error::Io(_) => (StatusCode::INTERNAL_SERVER_ERROR, "storage_error"),
        };
        let body = Json(json!({ "error": code, "message": self.to_string() }));
        (status, body).into_response()
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn healthz(State(svc): State<Service>) -> Result<Json<serde_json::Value>, Error> {
    let v = run(move || svc.current_version()).await?;
    Ok(Json(json!({ "status": "ok", "current_version": v })))
}

async fn info(State(svc): State<Service>) -> Result<Json<serde_json::Value>, Error> {
    run(move || {
        let v = svc.current_version()?;
        let info = svc
            .version_info(v)?
            .ok_or_else(|| Error::Storage("current version has no manifest".into()))?;
        Ok(json!({
            "service": "merkle-proof-service",
            "current_version": v,
            "current_root": Hex32(info.root),
            "leaf_count": info.leaf_count,
            "hash": "SHA-256",
            "leaf_encoding": "SHA256(0x00 || u32be(|key|) || key || u32be(|value|) || value)",
            "branch_encoding": "SHA256(0x01 || left(32) || right(32))",
            "empty_root": "SHA256(0x02)",
        }))
    })
    .await
    .map(Json)
}

async fn list_versions(State(svc): State<Service>) -> Result<Json<serde_json::Value>, Error> {
    run(move || {
        let infos = svc.list_versions()?;
        let roots: Vec<RootDto> = infos
            .into_iter()
            .map(|i| RootDto {
                version: i.version,
                root: Hex32(i.root),
                leaf_count: i.leaf_count,
                created_at_unix_ns: i.created_at_unix_ns,
            })
            .collect();
        Ok(json!({ "versions": roots }))
    })
    .await
    .map(Json)
}

async fn root_of(
    State(svc): State<Service>,
    Path(version): Path<u64>,
) -> Result<Json<RootDto>, Error> {
    run(move || {
        let info = svc.version_info(version)?.ok_or(Error::UnknownVersion(version))?;
        Ok(RootDto {
            version: info.version,
            root: Hex32(info.root),
            leaf_count: info.leaf_count,
            created_at_unix_ns: info.created_at_unix_ns,
        })
    })
    .await
    .map(Json)
}

async fn apply_batch(
    State(svc): State<Service>,
    AppJson(req): AppJson<BatchRequest>,
) -> Result<Json<serde_json::Value>, Error> {
    if req.ops.len() > 1_000_000 {
        return Err(Error::bad("batch too large (limit 1,000,000 ops)"));
    }
    let ops: Vec<WriteOp> = req
        .ops
        .into_iter()
        .map(|item| match item {
            BatchItemDto::Put { key, value } => {
                WriteOp::Put { key: key.0, value: value.0 }
            }
            BatchItemDto::Delete { key } => WriteOp::Delete { key: key.0 },
        })
        .collect();

    let info = svc.apply_batch(ops).await?;
    Ok(Json(json!({
        "version": info.version,
        "root": Hex32(info.root),
        "leaf_count": info.leaf_count,
        "created_at_unix_ns": info.created_at_unix_ns,
    })))
}

#[derive(Debug, Serialize)]
struct KeyResponse {
    key: HexBytes,
    exists: bool,
    /// Present only when exists; an empty string means an explicit empty value.
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<HexBytes>,
}

async fn get_key(
    State(svc): State<Service>,
    Path(key_hex): Path<String>,
) -> Result<Json<KeyResponse>, Error> {
    let key = parse_hex(&key_hex)?;
    let value = run({
        let key = key.clone();
        move || svc.get(&key)
    })
    .await?;
    Ok(Json(match value {
        Some(v) => KeyResponse {
            key: HexBytes(key),
            exists: true,
            value: Some(HexBytes(v)),
        },
        None => KeyResponse { key: HexBytes(key), exists: false, value: None },
    }))
}

async fn get_proof(
    State(svc): State<Service>,
    Path(key_hex): Path<String>,
    Query(q): Query<VersionQuery>,
) -> Result<Json<ProofResponse>, Error> {
    let key = parse_hex(&key_hex)?;
    run(move || svc.prove(&key, q.version)).await.map(Json)
}

async fn verify(
    AppJson(req): AppJson<VerifyRequest>,
) -> Result<Json<serde_json::Value>, Error> {
    let outcome: std::result::Result<(), String> = match req {
        VerifyRequest::Inclusion { root, proof } => {
            verify_inclusion(&proof, root.as_hash()).map_err(|e| e.to_string())
        }
        VerifyRequest::NonExistence { root, proof } => {
            verify_non_existence(&proof, root.as_hash()).map_err(|e| e.to_string())
        }
        VerifyRequest::Response { root, response } => {
            crate::proof::verify_response(&response, root.as_hash()).map_err(|e| e.to_string())
        }
    };
    Ok(Json(match outcome {
        Ok(()) => json!({ "valid": true, "reason": null }),
        Err(reason) => json!({ "valid": false, "reason": reason }),
    }))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn parse_hex(s: &str) -> Result<Vec<u8>, Error> {
    let s = s.strip_prefix("0x").unwrap_or(s);
    hex::decode(s).map_err(|e| Error::bad(format!("invalid hex key: {e}")))
}

/// Run a blocking RocksDB operation on the blocking pool.
async fn run<F, T>(f: F) -> Result<T, Error>
where
    F: FnOnce() -> Result<T, Error> + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| Error::Storage(format!("worker panicked: {e}")))?
}
