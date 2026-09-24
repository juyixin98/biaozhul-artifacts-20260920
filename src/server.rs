//! Axum HTTP layer exposing one index instance behind a mutex.
//!
//! All storage operations are synchronous `pwrite`/`fdatasync` calls wrapped
//! in `spawn_blocking`; the mutex serializes structural mutations as required
//! by the engine (the directory is an in-memory cache of the on-disk run).
use crate::error::IndexError;
use crate::index::{Index, Stats};
use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};

#[derive(Clone)]
pub struct AppState {
    pub index: Arc<Mutex<Index>>,
}

pub fn router(index: Index) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/stats", get(stats))
        .route("/keys/{key}", get(get_key).put(put_key).delete(delete_key))
        .with_state(AppState {
            index: Arc::new(Mutex::new(index)),
        })
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

#[derive(Deserialize)]
pub struct PutBody {
    /// Accepted encodings: a JSON string (stored as UTF-8), or
    /// `{"hex": ".."}` / `{"base64": ".."}` for raw bytes.
    pub value: serde_json::Value,
}

#[derive(Serialize)]
struct PutResponse {
    inserted: bool,
    key: String,
}

#[derive(Serialize)]
struct GetResponse {
    key: String,
    found: bool,
    value: Option<serde_json::Value>,
}

#[derive(Serialize)]
struct DeleteResponse {
    key: String,
    deleted: bool,
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
    code: &'static str,
}

fn err(status: StatusCode, code: &'static str, error: String) -> Response {
    (status, Json(ErrorBody { error, code })).into_response()
}

fn map_error(e: IndexError) -> Response {
    match &e {
        IndexError::CollisionCapacity { .. } => err(
            StatusCode::INSUFFICIENT_STORAGE,
            "hash_collision_capacity",
            e.to_string(),
        ),
        IndexError::DepthLimit { .. } => err(
            StatusCode::INSUFFICIENT_STORAGE,
            "directory_depth_limit",
            e.to_string(),
        ),
        IndexError::EntryTooLarge { .. } => err(
            StatusCode::PAYLOAD_TOO_LARGE,
            "entry_too_large",
            e.to_string(),
        ),
        IndexError::Config(_) => err(
            StatusCode::UNPROCESSABLE_ENTITY,
            "config_error",
            e.to_string(),
        ),
        IndexError::Corrupt(_) => err(
            StatusCode::INTERNAL_SERVER_ERROR,
            "corrupt_index",
            e.to_string(),
        ),
        IndexError::Io(_) => err(StatusCode::INTERNAL_SERVER_ERROR, "io_error", e.to_string()),
    }
}

/// Stored bytes return as a JSON string when valid UTF-8, otherwise as
/// `{"hex": ".."}`.
fn value_to_json(v: &[u8]) -> serde_json::Value {
    match std::str::from_utf8(v) {
        Ok(s) => serde_json::Value::String(s.to_string()),
        Err(_) => serde_json::json!({ "hex": hex_encode(v) }),
    }
}

fn hex_encode(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

fn json_value_to_bytes(v: &serde_json::Value) -> Result<Vec<u8>, String> {
    match v {
        serde_json::Value::String(s) => Ok(s.as_bytes().to_vec()),
        serde_json::Value::Object(map) => {
            if let Some(hex) = map.get("hex").and_then(|x| x.as_str()) {
                hex_decode(hex).ok_or_else(|| "invalid hex string".to_string())
            } else if let Some(b64) = map.get("base64").and_then(|x| x.as_str()) {
                base64_decode(b64).ok_or_else(|| "invalid base64 string".to_string())
            } else {
                Err("value object must contain \"hex\" or \"base64\"".to_string())
            }
        }
        _ => Err("value must be a JSON string or a {\"hex\"|\"base64\"} object".to_string()),
    }
}

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    let bytes = s.as_bytes();
    if !bytes.len().is_multiple_of(2) {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for pair in bytes.as_chunks::<2>().0 {
        let hi = (pair[0] as char).to_digit(16)?;
        let lo = (pair[1] as char).to_digit(16)?;
        out.push((hi * 16 + lo) as u8);
    }
    Some(out)
}

/// Standard-alphabet base64 decoder; padding optional, whitespace ignored.
fn base64_decode(s: &str) -> Option<Vec<u8>> {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut lut = [255u8; 256];
    for (i, &c) in ALPHABET.iter().enumerate() {
        lut[c as usize] = i as u8;
    }
    let mut out = Vec::new();
    let mut acc: Vec<u8> = Vec::with_capacity(4);
    let mut pad = 0u32;
    for &c in s.as_bytes() {
        if c == b'\n' || c == b'\r' || c == b' ' || c == b'\t' {
            continue;
        }
        if c == b'=' {
            pad += 1;
            acc.push(0);
        } else {
            if pad > 0 || lut[c as usize] == 255 {
                return None;
            }
            acc.push(lut[c as usize]);
        }
        if acc.len() == 4 {
            let (a, b, cc, d) = (acc[0], acc[1], acc[2], acc[3]);
            out.push((a << 2) | (b >> 4));
            if pad < 2 {
                out.push((b << 4) | (cc >> 2));
            }
            if pad == 0 {
                out.push((cc << 6) | d);
            }
            acc.clear();
        }
    }
    if !acc.is_empty() || pad > 2 {
        return None;
    }
    Some(out)
}

async fn get_key(
    State(st): State<AppState>,
    axum::extract::Path(key): axum::extract::Path<String>,
) -> Response {
    let idx = st.index.clone();
    let k = key.clone();
    let result = tokio::task::spawn_blocking(move || idx.lock().unwrap().get(k.as_bytes()))
        .await
        .unwrap();
    match result {
        Ok(Some(v)) => Json(GetResponse {
            key,
            found: true,
            value: Some(value_to_json(&v)),
        })
        .into_response(),
        Ok(None) => Json(GetResponse {
            key,
            found: false,
            value: None,
        })
        .into_response(),
        Err(e) => map_error(e),
    }
}

async fn put_key(
    State(st): State<AppState>,
    axum::extract::Path(key): axum::extract::Path<String>,
    Json(body): Json<PutBody>,
) -> Response {
    let value_bytes = match json_value_to_bytes(&body.value) {
        Ok(b) => b,
        Err(msg) => return err(StatusCode::BAD_REQUEST, "bad_value", msg),
    };
    let idx = st.index.clone();
    let result = tokio::task::spawn_blocking(move || {
        idx.lock()
            .unwrap()
            .put(key.as_bytes(), &value_bytes)
            .map(|inserted| PutResponse { inserted, key })
    })
    .await
    .unwrap();
    match result {
        Ok(r) => Json(r).into_response(),
        Err(e) => map_error(e),
    }
}

async fn delete_key(
    State(st): State<AppState>,
    axum::extract::Path(key): axum::extract::Path<String>,
) -> Response {
    let idx = st.index.clone();
    let k = key.clone();
    let result = tokio::task::spawn_blocking(move || {
        idx.lock()
            .unwrap()
            .delete(k.as_bytes())
            .map(|deleted| DeleteResponse { deleted, key })
    })
    .await
    .unwrap();
    match result {
        Ok(r) => Json(r).into_response(),
        Err(e) => map_error(e),
    }
}

async fn stats(State(st): State<AppState>) -> Response {
    let idx = st.index.clone();
    let result = tokio::task::spawn_blocking(move || idx.lock().unwrap().stats()).await;
    match result {
        Ok(Ok(s)) => {
            let v: Stats = s;
            Json(v).into_response()
        }
        Ok(Err(e)) => map_error(e),
        Err(e) => err(
            StatusCode::INTERNAL_SERVER_ERROR,
            "worker_panic",
            e.to_string(),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_known_vectors() {
        assert_eq!(base64_decode("").unwrap(), b"");
        assert_eq!(base64_decode("Zg==").unwrap(), b"f");
        assert_eq!(base64_decode("Zm8=").unwrap(), b"fo");
        assert_eq!(base64_decode("Zm9v").unwrap(), b"foo");
        assert_eq!(base64_decode("Zm9vYmFy").unwrap(), b"foobar");
        assert_eq!(hex_decode("00ff").unwrap(), vec![0x00, 0xff]);
    }
}
