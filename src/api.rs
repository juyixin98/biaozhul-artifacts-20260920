//! HTTP surface: selection endpoint plus local-only content administration.

use std::sync::{Arc, Mutex};

use axum::{
    extract::{Query, State},
    http::StatusCode,
    routing::{get, post, put},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::digest::Digest;
use crate::error::{ApiError, ApiResult};
use crate::model::BlobKind;
use crate::reference::Ref;
use crate::select::{select, PlatformRequest, Selection};
use crate::store::Store;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Mutex<Store>>,
}

impl AppState {
    pub fn new(store: Store) -> Self {
        AppState {
            store: Arc::new(Mutex::new(store)),
        }
    }
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/select", post(select_handler))
        .route("/admin/repos", get(list_repos).post(create_repo))
        .route("/admin/blobs", put(put_blob))
        .route("/admin/blobs/claim", put(put_blob_claimed))
        .route("/admin/blobs/blob", get(get_blob))
        .route("/admin/tags", put(put_tag))
        .route("/admin/dangling-tag", put(seed_dangling_tag))
        .with_state(state)
}

async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

#[derive(Debug, Deserialize)]
struct SelectQuery {
    reference: String,
}

async fn select_handler(
    State(state): State<AppState>,
    Query(q): Query<SelectQuery>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Selection>> {
    let reference = Ref::parse(&q.reference)?;
    let req: PlatformRequest = serde_json::from_value(body)
        .map_err(|e| ApiError::BadRequest(format!("invalid request body: {e}")))?;
    let store = state.store.lock().expect("store lock poisoned");
    let result = select(&store, &reference, &req)?;
    Ok(Json(result))
}

async fn list_repos(State(state): State<AppState>) -> Json<Value> {
    let store = state.store.lock().unwrap();
    let repos: Vec<Value> = store
        .repos
        .iter()
        .map(|(name, repo)| {
            json!({
                "name": name,
                "tags": repo.tags,
                "blobCount": repo.blobs.len(),
            })
        })
        .collect();
    Json(json!({ "repositories": repos }))
}

#[derive(Debug, Deserialize)]
struct CreateRepoReq {
    name: String,
}

async fn create_repo(
    State(state): State<AppState>,
    Json(req): Json<CreateRepoReq>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    if !req.name.contains('/') {
        return Err(ApiError::BadRequest(
            "repository name must contain at least one path component, e.g. \"demo/app\"".into(),
        ));
    }
    Ref::parse(&format!("{}:seed", req.name))?;
    let mut store = state.store.lock().unwrap();
    let existed = store.repos.contains_key(&req.name);
    store.repos.entry(req.name.clone()).or_default();
    Ok((
        if existed {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        Json(json!({ "name": req.name, "existed": existed })),
    ))
}

#[derive(Debug, Deserialize)]
struct BlobQuery {
    repository: String,
    digest: String,
}

#[derive(Debug, Deserialize)]
struct PutBlobReq {
    /// Repository to write into, e.g. `demo/app`.
    repository: String,
    /// Canonical OCI object (index/manifest). Serialized canonically and
    /// stored under its real digest.
    #[serde(default)]
    json: Option<Value>,
    /// Alternatively, raw (possibly non-JSON) bytes, base64 encoded.
    #[serde(default, rename = "rawBase64")]
    raw_base64: Option<String>,
}

#[allow(clippy::needless_pass_by_value)]
async fn put_blob(
    State(state): State<AppState>,
    Json(req): Json<PutBlobReq>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    validate_repository(&req.repository)?;
    let repo = req.repository;
    let bytes = match (req.json, req.raw_base64) {
        (Some(json), None) => serde_json::to_vec(&json).unwrap(),
        (None, Some(b64)) => crate::loader::base64_decode(&b64)
            .map_err(|e| ApiError::BadRequest(format!("bad rawBase64: {e}")))?,
        (Some(_), Some(_)) => {
            return Err(ApiError::BadRequest(
                "provide exactly one of \"json\" or \"rawBase64\"".into(),
            ));
        }
        (None, None) => {
            return Err(ApiError::BadRequest(
                "provide either \"json\" or \"rawBase64\"".into(),
            ));
        }
    };
    let digest = Digest::sha256(&bytes);
    let kind = BlobKind::classify(crate::model::known_media_type(&bytes), &bytes)
        .map_err(ApiError::BadRequest)?;

    let mut store = state.store.lock().unwrap();
    store
        .repo_mut(&repo)
        .blobs
        .entry(digest.to_string())
        .or_insert(crate::store::Blob {
            bytes: bytes.clone(),
            kind,
        });
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "digest": digest.to_string(),
            "size": bytes.len(),
        })),
    ))
}

#[derive(Debug, Deserialize)]
struct PutBlobClaimedReq {
    /// Repository to write into.
    repository: String,
    /// Digest under which the bytes will be keyed. The service records that
    /// this disagrees with the actual sha256; reading the blob therefore
    /// fails verification. Test/demo only — a real registry can never do
    /// this for intact content.
    #[serde(rename = "claimedDigest")]
    claimed_digest: String,
    #[serde(default)]
    json: Option<Value>,
    #[serde(default, rename = "rawBase64")]
    raw_base64: Option<String>,
}

/// Test-only: store content under an arbitrary digest key. The bytes are
/// still classified, but any later fetch reports `digest_mismatch`.
async fn put_blob_claimed(
    State(state): State<AppState>,
    Json(req): Json<PutBlobClaimedReq>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    validate_repository(&req.repository)?;
    let repo = req.repository;
    let claimed = Digest::parse(&req.claimed_digest).map_err(|e| ApiError::BadRequest(e.0))?;
    let bytes = match (req.json, req.raw_base64) {
        (Some(value), None) => serde_json::to_vec(&value).unwrap(),
        (None, Some(b64)) => crate::loader::base64_decode(&b64)
            .map_err(|e| ApiError::BadRequest(format!("bad rawBase64: {e}")))?,
        _ => {
            return Err(ApiError::BadRequest(
                "provide exactly one of \"json\" or \"rawBase64\"".into(),
            ));
        }
    };
    let actual = Digest::sha256(&bytes);
    let kind = BlobKind::classify(crate::model::known_media_type(&bytes), &bytes)
        .map_err(ApiError::BadRequest)?;
    let mut store = state.store.lock().unwrap();
    store
        .repo_mut(&repo)
        .blobs
        .insert(claimed.to_string(), crate::store::Blob { bytes, kind });
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "claimedDigest": claimed.to_string(),
            "actualDigest": actual.to_string(),
            "verifies": actual == claimed,
        })),
    ))
}

async fn get_blob(
    State(state): State<AppState>,
    Query(q): Query<BlobQuery>,
) -> ApiResult<Json<Value>> {
    validate_repository(&q.repository)?;
    let parsed = Digest::parse(&q.digest).map_err(|e| ApiError::BadRequest(e.0))?;
    let store = state.store.lock().unwrap();
    let blob = store.get(&q.repository, &parsed)?;
    let bytes = &blob.bytes;
    let value: Option<Value> = serde_json::from_slice(bytes).ok();
    Ok(Json(json!({
        "digest": parsed.to_string(),
        "size": bytes.len(),
        "json": value,
        "rawBase64": base64_encode(bytes),
    })))
}

#[derive(Debug, Deserialize)]
struct PutTagReq {
    repository: String,
    tag: String,
    digest: String,
}

async fn put_tag(
    State(state): State<AppState>,
    Json(req): Json<PutTagReq>,
) -> ApiResult<Json<Value>> {
    validate_repository(&req.repository)?;
    Digest::parse(&req.digest).map_err(|e| ApiError::BadRequest(e.0))?;
    let mut store = state.store.lock().unwrap();
    store.put_tag(&req.repository, req.tag.clone(), req.digest.clone())?;
    Ok(Json(json!({
        "repository": req.repository,
        "tag": req.tag,
        "digest": req.digest
    })))
}

#[derive(Debug, Deserialize)]
struct SeedDanglingReq {
    repository: String,
    tag: String,
    /// Digest the tag will claim. Bytes are stored separately under their
    /// real digest so resolution finds a tag but verification fails.
    #[serde(rename = "claimedDigest")]
    claimed_digest: String,
    #[serde(default)]
    raw_base64: Option<String>,
    json: Option<Value>,
}

/// Test-only helper: store content under its *real* digest while pointing a
/// tag at a *different* claimed digest, simulating a corrupt/tampered tag
/// without needing a broken on-disk fixture.
async fn seed_dangling_tag(
    State(state): State<AppState>,
    Json(req): Json<SeedDanglingReq>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    validate_repository(&req.repository)?;
    let repo = req.repository;
    let claimed = Digest::parse(&req.claimed_digest).map_err(|e| ApiError::BadRequest(e.0))?;
    let bytes = match (req.json, req.raw_base64) {
        (Some(v), None) => serde_json::to_vec(&v).unwrap(),
        (None, Some(b64)) => crate::loader::base64_decode(&b64)
            .map_err(|e| ApiError::BadRequest(format!("bad rawBase64: {e}")))?,
        _ => {
            return Err(ApiError::BadRequest(
                "provide exactly one of \"json\" or \"rawBase64\"".into(),
            ));
        }
    };
    let real = Digest::sha256(&bytes);
    if real == claimed {
        return Err(ApiError::BadRequest(
            "claimedDigest equals the actual digest; nothing corrupt to seed".into(),
        ));
    }
    let kind = BlobKind::classify(None, &bytes).map_err(ApiError::BadRequest)?;
    let mut store = state.store.lock().unwrap();
    store
        .repo_mut(&repo)
        .blobs
        .insert(real.to_string(), crate::store::Blob { bytes, kind });
    store
        .repo_mut(&repo)
        .tags
        .insert(req.tag.clone(), claimed.to_string());
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "tag": req.tag,
            "claimedDigest": claimed.to_string(),
            "actualStoredDigest": real.to_string(),
        })),
    ))
}

/// Validate a repository name by round-tripping it through [`Ref`].
fn validate_repository(name: &str) -> ApiResult<()> {
    // A repository alone parses with implicit `:latest`; this validates the
    // repository grammar without requiring a tag to exist.
    let probe = format!("{name}:probe");
    Ref::parse(&probe).map(|_| ())
}

fn base64_encode(bytes: &[u8]) -> String {
    const CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0];
        let b1 = if chunk.len() > 1 { chunk[1] } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] } else { 0 };
        out.push(CHARS[b0 as usize >> 2] as char);
        out.push(CHARS[((b0 & 0x03) << 4 | b1 >> 4) as usize] as char);
        if chunk.len() > 1 {
            out.push(CHARS[((b1 & 0x0f) << 2 | b2 >> 6) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(CHARS[(b2 & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}
