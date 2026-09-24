//! HTTP API (pure JSON/multipart, no UI).
//!
//! Endpoints:
//!
//! | Method | Path                    | Purpose                                   |
//! |--------|-------------------------|-------------------------------------------|
//! | GET    | `/healthz`              | liveness                                  |
//! | PUT    | `/v1/artifacts/:id`     | upload raw artifact bytes                 |
//! | GET    | `/v1/artifacts/:id`     | download raw artifact bytes               |
//! | POST   | `/v1/digests`           | BLAKE3 hex digest of a raw uploaded body  |
//! | POST   | `/v1/signatures`        | build a block signature for a basis       |
//! | POST   | `/v1/deltas`            | compute delta (signature + new content)   |
//! | POST   | `/v1/patch`             | apply a delta to a stored basis           |

use axum::extract::{multipart::Field, Multipart, Path, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, put};
use axum::{Json, Router};
use base64::Engine;
use serde_json::{json, Value};

use crate::checksum;
use crate::delta;
use crate::protocol::{Delta, Op, Signature};
use crate::store::Store;
use crate::Error;

/// Limit for individual form fields that carry JSON (generous: up to 256 MiB
/// of base64 signature data; way above any practical test size).
const MAX_JSON_FIELD: usize = 256 * 1024 * 1024;
/// Limit for an uploaded artifact field.
const MAX_DATA_FIELD: usize = 1024 * 1024 * 1024;

#[derive(Clone)]
pub struct AppState {
    pub store: std::sync::Arc<Store>,
}

pub fn app(store: std::sync::Arc<Store>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/artifacts/{id}", put(put_artifact).get(get_artifact))
        .route("/v1/digests", axum::routing::post(post_digests))
        .route("/v1/signatures", axum::routing::post(post_signatures))
        .route("/v1/deltas", axum::routing::post(post_deltas))
        .route("/v1/patch", axum::routing::post(post_patch))
        .with_state(AppState { store })
}

async fn healthz() -> &'static str {
    "ok\n"
}

/// Artifact IDs: 1..=128 chars from [A-Za-z0-9._-].
fn validate_artifact_id(id: &str) -> Result<(), Error> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Err(Error::bad_request(
            "artifact id must be 1-128 chars of [A-Za-z0-9._-]",
        ));
    }
    Ok(())
}

async fn put_artifact(
    State(state): State<AppState>,
    Path(id): Path<String>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, Error> {
    validate_artifact_id(&id)?;
    let len = body.len();
    state.store.put(&id, body.to_vec())?;
    Ok(Json(json!({ "id": id, "bytes": len })))
}

async fn get_artifact(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<impl IntoResponse, Error> {
    validate_artifact_id(&id)?;
    let data = state
        .store
        .get(&id)
        .ok_or_else(|| Error::not_found(format!("artifact {id:?} not found")))?;
    Ok((
        [(header::CONTENT_TYPE, "application/octet-stream")],
        data,
    ))
}

// ----- multipart helpers -----------------------------------------------------

async fn read_field(field: &mut Field<'_>, max: usize, what: &str) -> Result<Vec<u8>, Error> {
    let mut buf = Vec::new();
    while let Some(chunk) = field.chunk().await? {
        if buf.len() + chunk.len() > max {
            return Err(Error::payload_too_large(format!(
                "{what} field exceeds {max} bytes"
            )));
        }
        buf.extend_from_slice(&chunk);
    }
    Ok(buf)
}

async fn parse_multipart(mut mp: Multipart) -> Result<MultipartFields, Error> {
    let mut fields = MultipartFields::default();
    while let Some(mut field) = mp.next_field().await? {
        match field.name().unwrap_or("") {
            "basis_id" => fields.basis_id = Some(String::from_utf8(read_field(&mut field, 4096, "basis_id").await?).map_err(|e| Error::bad_request(format!("basis_id not UTF-8: {e}")))?),
            "block_size" => fields.block_size = Some(String::from_utf8(read_field(&mut field, 32, "block_size").await?).map_err(|e| Error::bad_request(format!("block_size not UTF-8: {e}")))?.trim().to_string()),
            "signature" => fields.signature = Some(read_field(&mut field, MAX_JSON_FIELD, "signature").await?),
            "delta" => fields.delta = Some(read_field(&mut field, MAX_JSON_FIELD, "delta").await?),
            "data" => fields.data = Some(read_field(&mut field, MAX_DATA_FIELD, "data").await?),
            "expected_blake3_hex" => fields.expected_blake3_hex = Some(String::from_utf8(read_field(&mut field, 256, "expected_blake3_hex").await?).map_err(|e| Error::bad_request(format!("expected_blake3_hex not UTF-8: {e}")))?.trim().to_string()),
            other => return Err(Error::bad_request(format!("unexpected multipart field {other:?}"))),
        }
    }
    Ok(fields)
}

#[derive(Default)]
struct MultipartFields {
    basis_id: Option<String>,
    block_size: Option<String>,
    signature: Option<Vec<u8>>,
    delta: Option<Vec<u8>>,
    data: Option<Vec<u8>>,
    expected_blake3_hex: Option<String>,
}

impl MultipartFields {
    fn require<T>(v: Option<T>, name: &str) -> Result<T, Error> {
        v.ok_or_else(|| Error::bad_request(format!("missing multipart field {name:?}")))
    }
}

fn parse_block_size(raw: Option<String>) -> Result<usize, Error> {
    match raw {
        Some(s) => {
            let n: usize = s
                .parse()
                .map_err(|_| Error::bad_request("block_size must be an integer"))?;
            if !(checksum::MIN_BLOCK_SIZE..=checksum::MAX_BLOCK_SIZE).contains(&n) {
                return Err(Error::bad_request(format!(
                    "block_size must be in {}..={}",
                    checksum::MIN_BLOCK_SIZE,
                    checksum::MAX_BLOCK_SIZE
                )));
            }
            Ok(n)
        }
        None => Ok(1024),
    }
}

fn basis_from_state(state: &AppState, basis_id: &str) -> Result<Vec<u8>, Error> {
    state
        .store
        .get(basis_id)
        .ok_or_else(|| Error::not_found(format!("basis artifact {basis_id:?} not found")))
}

// ----- /v1/digests -----------------------------------------------------------

/// BLAKE3 hex digest of an arbitrary raw request body. This lets thin clients
/// (curl + coreutils) produce the `expected_blake3_hex` for `/v1/patch`
/// without a local BLAKE3 implementation.
async fn post_digests(body: axum::body::Bytes) -> Json<Value> {
    Json(json!({
        "blake3_hex": crate::hex::encode(&checksum::strong(&body)),
        "bytes": body.len(),
    }))
}

// ----- /v1/signatures --------------------------------------------------------

async fn post_signatures(
    State(state): State<AppState>,
    mp: Multipart,
) -> Result<Json<Value>, Error> {
    let f = parse_multipart(mp).await?;
    let basis_id = MultipartFields::require(f.basis_id, "basis_id")?;
    let block_size = parse_block_size(f.block_size)?;
    let basis = basis_from_state(&state, &basis_id)?;
    let sig = Signature::build(&basis, block_size);
    Ok(Json(json!({
        "signature": sig,
        "basis_id": basis_id,
        "basis_bytes": basis.len(),
        "blocks": sig.blocks.len(),
    })))
}

// ----- /v1/deltas ------------------------------------------------------------

async fn post_deltas(mp: Multipart) -> Result<impl IntoResponse, Error> {
    let f = parse_multipart(mp).await?;
    let signature_raw = MultipartFields::require(f.signature, "signature")?;
    let data = MultipartFields::require(f.data, "data")?;

    // The signature is self-describing; the referenced basis need not live on
    // this server (the protocol is stateless end-to-end).
    let sig: Signature = serde_json::from_slice(&signature_raw)
        .map_err(|e| Error::bad_request(format!("invalid signature JSON: {e}")))?;
    if !(checksum::MIN_BLOCK_SIZE..=checksum::MAX_BLOCK_SIZE).contains(&sig.block_size) {
        return Err(Error::bad_request(format!(
            "signature.block_size must be in {}..={}",
            checksum::MIN_BLOCK_SIZE,
            checksum::MAX_BLOCK_SIZE
        )));
    }

    let d = delta::compute_delta(&data, &sig)?;

    let literal_bytes = d.literal_bytes();
    let copy_blocks = d.copy_count();
    // Exact reused bytes: a COPY of the (possibly short) last block contributes
    // its real length, not the full block_size.
    let copied_bytes: usize = d
        .ops
        .iter()
        .map(|op| match op {
            Op::Copy { block_index } => {
                let start = block_index.saturating_mul(d.block_size);
                sig.basis_len.saturating_sub(start).min(d.block_size)
            }
            Op::Literal { .. } => 0,
        })
        .sum();
    // literal + copied must partition the target exactly (every emitted output
    // byte is one or the other).
    debug_assert_eq!(literal_bytes + copied_bytes, data.len());
    let target_bytes = data.len();
    let full_transfer_bytes = target_bytes; // sending the whole new file
    // Wire size = exact JSON encoding of the delta object.
    let wire_bytes = serde_json::to_vec(&json!({
        "block_size": d.block_size,
        "basis_len": d.basis_len,
        "ops": d.ops,
    }))
    .map_err(|e| Error::bad_request(format!("delta serialization failed: {e}")))?
    .len();
    let raw_saved = full_transfer_bytes.saturating_sub(literal_bytes);
    // Signed on purpose: for files with no shared blocks the base64+JSON
    // envelope makes the delta LARGER than a raw full transfer. We report the
    // real (negative) number rather than clamping it to zero.
    let wire_saved: i64 = full_transfer_bytes as i64 - wire_bytes as i64;
    let wire_saved_pct: f64 = if full_transfer_bytes > 0 {
        (wire_saved as f64 / full_transfer_bytes as f64) * 100.0
    } else {
        0.0
    };

    let mut headers = HeaderMap::new();
    let put = |h: &mut HeaderMap, name: &'static str, v: usize| {
        if let Ok(val) = HeaderValue::from_str(&v.to_string()) {
            h.insert(name, val);
        }
    };
    put(&mut headers, "x-target-bytes", target_bytes);
    put(&mut headers, "x-literal-bytes", literal_bytes);
    put(&mut headers, "x-copied-bytes", copied_bytes);
    put(&mut headers, "x-copy-blocks", copy_blocks);
    put(&mut headers, "x-delta-wire-bytes", wire_bytes);
    put(&mut headers, "x-full-transfer-bytes", full_transfer_bytes);
    put(&mut headers, "x-raw-saved-bytes", raw_saved);
    headers.insert(
        "x-wire-saved-bytes",
        HeaderValue::from_str(&wire_saved.to_string()).unwrap(),
    );
    headers.insert(
        "x-wire-saved-percent",
        HeaderValue::from_str(&format!("{wire_saved_pct:.2}")).unwrap(),
    );

    let body = Json(json!({
        "delta": d,
        "stats": {
            "target_bytes": target_bytes,
            "literal_bytes": literal_bytes,
            "copied_bytes": copied_bytes,
            "copy_blocks": copy_blocks,
            "delta_wire_bytes": wire_bytes,
            "full_transfer_bytes": full_transfer_bytes,
            "raw_saved_bytes": raw_saved,
            "wire_saved_bytes": wire_saved,
            "wire_saved_percent": (wire_saved_pct * 100.0).round() / 100.0,
        }
    }));
    Ok((StatusCode::OK, headers, body))
}

// ----- /v1/patch -------------------------------------------------------------

async fn post_patch(State(state): State<AppState>, mp: Multipart) -> Result<Json<Value>, Error> {
    let f = parse_multipart(mp).await?;
    let basis_id = MultipartFields::require(f.basis_id, "basis_id")?;
    let delta_raw = MultipartFields::require(f.delta, "delta")?;
    let expected_hex = MultipartFields::require(f.expected_blake3_hex, "expected_blake3_hex")?;

    let basis = basis_from_state(&state, &basis_id)?;
    let d: Delta = serde_json::from_slice(&delta_raw)
        .map_err(|e| Error::bad_request(format!("invalid delta JSON: {e}")))?;

    let result = delta::apply_delta(&basis, &d)?;
    let digest = checksum::strong_whole(&result);
    let actual_hex = crate::hex::encode(&digest);

    let expected = crate::hex::decode(expected_hex.trim())
        .map_err(|e| Error::bad_request(format!("invalid expected_blake3_hex: {e}")))?;
    if expected.len() != 32 {
        return Err(Error::bad_request(
            "expected_blake3_hex must be 32 bytes (64 hex chars)",
        ));
    }
    if expected != digest.to_vec() {
        return Err(Error {
            status: StatusCode::CONFLICT,
            message: format!(
                "reconstructed artifact digest mismatch: expected {expected_hex}, got {actual_hex}"
            ),
        });
    }

    Ok(Json(json!({
        "output_b64": base64::engine::general_purpose::STANDARD.encode(&result),
        "output_bytes": result.len(),
        "blake3_hex": actual_hex,
        "verified": true,
    })))
}
