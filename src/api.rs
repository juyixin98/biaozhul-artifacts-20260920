//! HTTP API (Axum).
//!
//! Protocol overview
//! -----------------
//! 1. `PUT  /artifacts/:name` — upload a new artifact version (raw bytes)
//! 2. `GET  /artifacts/:name` — download it
//! 3. `HEAD /artifacts/:name` — length + blake3 headers
//! 4. `GET  /artifacts/:name/signature?block_len=` — block signature of that
//!    artifact (convenience; clients may also compute signatures locally)
//! 5. `POST /artifacts/:name/delta` — body = signature of the client's OLD
//!    copy; optional request header `x-basis-blake3` names the digest of
//!    those old bytes; response = binary patch + stats headers
//! 6. `POST /artifacts/:name/apply?basis=<name>&store=true` — body = patch;
//!    reconstructs against a stored basis, optionally stores
//!
//! All hashes are BLAKE3 hex. The weak rolling checksum appears only inside
//! signature records and is never treated as proof of content equality.

use crate::delta::diff;
use crate::error::Error;
use crate::patch::{apply_patch, Patch};
use crate::signature::{default_block_len, strong_hash, Signature};
use crate::store::ArtifactStore;
use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<ArtifactStore>,
}

pub fn app(store: Arc<ArtifactStore>) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/", get(index))
        .route("/artifacts", get(list_artifacts))
        .route(
            "/artifacts/{name}",
            put(put_artifact).get(get_artifact).head(head_artifact),
        )
        .route("/artifacts/{name}/signature", get(get_signature))
        .route("/artifacts/{name}/delta", post(delta_handler))
        .route("/artifacts/{name}/apply", post(apply_handler))
        .with_state(state)
}

async fn index() -> impl IntoResponse {
    Json(json!({
        "service": "artifact-delta",
        "endpoints": [
            "PUT  /artifacts/:name",
            "GET  /artifacts/:name",
            "HEAD /artifacts/:name",
            "GET  /artifacts/:name/signature?block_len=",
            "POST /artifacts/:name/delta  (header x-basis-blake3 optional)",
            "POST /artifacts/:name/apply?basis=<stored>&store=true",
            "GET  /artifacts",
        ],
    }))
}

async fn list_artifacts(State(st): State<AppState>) -> impl IntoResponse {
    Json(json!({ "artifacts": st.store.list() }))
}

async fn put_artifact(
    State(st): State<AppState>,
    Path(name): Path<String>,
    body: Bytes,
) -> Result<impl IntoResponse, Error> {
    let len = st.store.put(&name, body.to_vec())?;
    let (_, hash) = st.store.meta(&name)?;
    Ok((
        StatusCode::OK,
        [
            ("x-target-length", len.to_string()),
            ("x-blake3", hash),
        ],
        Json(json!({ "status": "stored", "length": len })),
    ))
}

async fn get_artifact(
    State(st): State<AppState>,
    Path(name): Path<String>,
) -> Result<Response, Error> {
    let data = st.store.get(&name)?;
    let hash = hex_hash(&strong_hash(&data));
    let mut h = HeaderMap::new();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/octet-stream"),
    );
    h.insert("x-blake3", HeaderValue::from_str(&hash).unwrap());
    h.insert(
        "x-target-length",
        HeaderValue::from_str(&data.len().to_string()).unwrap(),
    );
    Ok((StatusCode::OK, h, data).into_response())
}

async fn head_artifact(
    State(st): State<AppState>,
    Path(name): Path<String>,
) -> Result<Response, Error> {
    let (len, hash) = st.store.meta(&name)?;
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header("x-blake3", hash)
        .header("x-target-length", len)
        .header(header::CONTENT_LENGTH, 0)
        .body(axum::body::Body::empty())
        .unwrap())
}

#[derive(Deserialize)]
struct BlockLenQuery {
    block_len: Option<u32>,
}

async fn get_signature(
    State(st): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<BlockLenQuery>,
) -> Result<Response, Error> {
    let basis = st.store.get(&name)?;
    let block_len = q.block_len.unwrap_or_else(|| default_block_len(basis.len() as u64));
    let sig = Signature::build(&basis, block_len)?;
    let bytes = sig.encode();
    let mut h = HeaderMap::new();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/vnd.artifact-delta.signature"),
    );
    h.insert("x-block-len", HeaderValue::from_str(&block_len.to_string()).unwrap());
    h.insert(
        "x-basis-length",
        HeaderValue::from_str(&basis.len().to_string()).unwrap(),
    );
    h.insert("x-blake3", HeaderValue::from_str(&hex_hash(&strong_hash(&basis))).unwrap());
    Ok((StatusCode::OK, h, bytes).into_response())
}

async fn delta_handler(
    State(st): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<impl IntoResponse, Error> {
    let target = st.store.get(&name)?;
    let sig = Signature::decode(&body)?;

    // Recover basis length from the records: index of the last block * S + len.
    let basis_len = sig
        .blocks
        .last()
        .map(|b| b.index as u64 * sig.block_len as u64 + b.length as u64)
        .unwrap_or(0);

    // Basis identity: trust the explicit client header; otherwise search the
    // store for an artifact that reproduces exactly this signature.
    let basis_hash = match headers.get("x-basis-blake3") {
        Some(hv) => parse_hex32(hv.to_str().map_err(|_| Error::BadName)?)?,
        None => identify_basis(&st, &sig)?,
    };

    let delta = diff(&sig, basis_len, &target);
    let patch = Patch::new(&delta, sig.block_len, basis_len, basis_hash);
    let patch_bytes = patch.encode();

    let s = &delta.stats;
    // Percent of the new artifact delivered as old-block references.
    let reuse_pct = if s.target_len > 0 {
        s.bytes_from_basis as f64 * 100.0 / s.target_len as f64
    } else {
        100.0
    };
    let mut h = HeaderMap::new();
    let put = |h: &mut HeaderMap, name: &'static str, val: String| {
        h.insert(name, HeaderValue::from_str(&val).unwrap());
    };
    put(&mut h, "x-target-length", s.target_len.to_string());
    put(&mut h, "x-basis-length", s.basis_len.to_string());
    put(&mut h, "x-blocks-matched", s.blocks_matched.to_string());
    put(&mut h, "x-bytes-from-basis", s.bytes_from_basis.to_string());
    put(&mut h, "x-literal-bytes", s.literal_bytes.to_string());
    put(&mut h, "x-weak-hits", s.weak_hits.to_string());
    put(&mut h, "x-strong-rejections", s.strong_rejections.to_string());
    put(&mut h, "x-patch-bytes", patch_bytes.len().to_string());
    put(&mut h, "x-reuse-percent", format!("{reuse_pct:.2}"));
    put(&mut h, "x-blake3", hex_hash(&delta.target_hash));
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/vnd.artifact-delta.patch"),
    );
    Ok((StatusCode::OK, h, patch_bytes).into_response())
}

/// Search stored artifacts for one whose signature (same block layout) is
/// identical to `sig`; return its digest. Returns zeros when not found — the
/// patch's target hash still guarantees end-to-end correctness on apply.
fn identify_basis(st: &AppState, sig: &Signature) -> Result<[u8; 32], Error> {
    for value in st.store.list() {
        let n = value["name"].as_str().unwrap_or("");
        if let Ok(data) = st.store.get(n) {
            if let Ok(theirs) = Signature::build(&data, sig.block_len) {
                if theirs.blocks.len() == sig.blocks.len()
                    && theirs.blocks.iter().zip(&sig.blocks).all(|(a, b)| {
                        a.index == b.index
                            && a.length == b.length
                            && a.weak == b.weak
                            && a.strong == b.strong
                    })
                {
                    return Ok(strong_hash(&data));
                }
            }
        }
    }
    Ok([0u8; 32])
}

fn parse_hex32(s: &str) -> Result<[u8; 32], Error> {
    let s = s.trim();
    if s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Error::Malformed("x-basis-blake3 must be 64 hex chars"));
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&s[2 * i..2 * i + 2], 16)
            .map_err(|_| Error::Malformed("bad hex digest"))?;
    }
    Ok(out)
}

#[derive(Deserialize)]
struct ApplyQuery {
    /// A stored artifact to use as the basis.
    basis: Option<String>,
    /// Store the reconstructed bytes under the path name on success.
    store: Option<bool>,
}

async fn apply_handler(
    State(st): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<ApplyQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<impl IntoResponse, Error> {
    let patch = Patch::decode(&body)?;
    let basis = if let Some(basis_name) = q.basis {
        st.store.get(&basis_name)?
    } else if let Some(hv) = headers.get("x-basis") {
        st.store.get(hv.to_str().map_err(|_| Error::BadName)?)?
    } else {
        return Err(Error::Malformed(
            "apply requires ?basis=<stored-name> or x-basis header",
        ));
    };
    let result = apply_patch(&patch, &basis)?;
    let hash = strong_hash(&result);
    if q.store.unwrap_or(false) {
        st.store.put(&name, result.clone())?;
    }
    let mut resp_headers = HeaderMap::new();
    resp_headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/octet-stream"),
    );
    resp_headers.insert("x-blake3", HeaderValue::from_str(&hex_hash(&hash)).unwrap());
    resp_headers.insert(
        "x-target-length",
        HeaderValue::from_str(&result.len().to_string()).unwrap(),
    );
    Ok::<_, Error>((StatusCode::OK, resp_headers, result))
}

fn hex_hash(h: &[u8; 32]) -> String {
    crate::store::hex(h)
}
