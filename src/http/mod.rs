//! HTTP replay service built with Axum.
//!
//! Raw recorded serial bytes are POSTed to the parser; no serial hardware is
//! involved.
//!
//! | Method | Path | Purpose |
//! |---|---|---|
//! | `GET`  | `/` | Service info |
//! | `POST` | `/parse` | Parse a complete raw body with a fresh parser (auto-finishes) |
//! | `POST` | `/session` | Create a stateful parser session for cross-request 半包 feeding |
//! | `POST` | `/session/{id}/feed`   | Feed an arbitrary byte slice |
//! | `POST` | `/session/{id}/finish` | Flush; report held bytes as truncated |
//! | `DELETE` | `/session/{id}` | Drop a session |
//! | `POST` | `/encode` | JSON `{sequence, payload_hex}` → `{frame_hex}` |
//! | `GET`  | `/sample` | Deterministic demo byte stream (`?format=hex` for JSON) |

use std::collections::HashMap;
use std::sync::{Mutex};

use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::IntoResponse,
    routing::{delete, get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::{encode_frame, Event, FrameParser};

#[derive(Clone)]
struct AppState {
    max_payload: usize,
    sessions: std::sync::Arc<Mutex<HashMap<String, FrameParser>>>,
    session_seq: std::sync::Arc<std::sync::atomic::AtomicU64>,
}

const MAX_SESSIONS: usize = 64;
const BODY_LIMIT: usize = 8 * 1024 * 1024;

pub fn app(max_payload: usize) -> Router {
    let state = AppState {
        max_payload,
        sessions: std::sync::Arc::new(Mutex::new(HashMap::new())),
        session_seq: std::sync::Arc::new(std::sync::atomic::AtomicU64::new(1)),
    };
    Router::new()
        .route("/", get(index))
        .route("/parse", post(parse_once))
        .route("/session", post(create_session))
        .route("/session/{id}/feed", post(feed_session))
        .route("/session/{id}/finish", post(finish_session))
        .route("/session/{id}", delete(drop_session))
        .route("/encode", post(encode))
        .route("/sample", get(sample))
        .layer(DefaultBodyLimit::max(BODY_LIMIT))
        .with_state(state)
}

async fn index(State(st): State<AppState>) -> Json<Value> {
    Json(json!({
        "service": "serial-frame-parser replay",
        "max_payload": st.max_payload,
        "frame_format": "magic DEADBEEF | len u16 BE | seq u16 BE | payload | crc32 u32 LE (CRC over len|seq|payload)",
        "endpoints": [
            "POST /parse",
            "POST /session",
            "POST /session/{id}/feed",
            "POST /session/{id}/finish",
            "DELETE /session/{id}",
            "POST /encode {sequence, payload_hex}",
            "GET /sample?format=hex"
        ]
    }))
}

fn events_to_json(events: &[Event]) -> Vec<Value> {
    events
        .iter()
        .map(|e| match e {
            Event::Frame { frame, offset } => json!({
                "type": "frame",
                "offset": offset,
                "sequence": frame.sequence,
                "payload_hex": hex_encode(&frame.payload),
                "payload_len": frame.payload.len(),
            }),
            Event::OversizedLength {
                declared_len,
                offset,
            } => json!({
                "type": "oversized_length",
                "offset": offset,
                "declared_len": declared_len,
            }),
            Event::BadCrc {
                sequence,
                declared_len,
                offset,
            } => json!({
                "type": "bad_crc",
                "offset": offset,
                "sequence": sequence,
                "declared_len": declared_len,
            }),
            Event::Noise { count, offset } => json!({
                "type": "noise",
                "offset": offset,
                "count": count,
            }),
            Event::SequenceGap {
                last,
                received,
                missing,
                offset,
            } => json!({
                "type": "sequence_gap",
                "offset": offset,
                "last": last,
                "received": received,
                "missing": missing,
            }),
            Event::OutOfOrder {
                received,
                expected,
                offset,
            } => json!({
                "type": "out_of_order",
                "offset": offset,
                "received": received,
                "expected": expected,
            }),
            Event::Truncated {
                bytes,
                has_magic,
                offset,
            } => json!({
                "type": "truncated",
                "offset": offset,
                "bytes_hex": hex_encode(bytes),
                "bytes_len": bytes.len(),
                "has_magic": has_magic,
            }),
            Event::BufferOverflow { offset } => json!({
                "type": "buffer_overflow",
                "offset": offset,
            }),
        })
        .collect()
}

fn stats_json(p: &FrameParser) -> Value {
    let s = p.stats();
    json!({
        "frames_ok": s.frames_ok,
        "bad_crc": s.bad_crc,
        "oversized": s.oversized,
        "sequence_gaps": s.sequence_gaps,
        "out_of_order": s.out_of_order,
        "truncated": s.truncated,
        "buffer_overflows": s.buffer_overflows,
        "bytes_total": s.bytes_total,
        "bytes_noise": s.bytes_noise,
        "buffered": p.buffer_len(),
    })
}

async fn parse_once(
    State(st): State<AppState>,
    Query(q): Query<ParseQuery>,
    body: Bytes,
) -> Json<Value> {
    let cap = q.max_payload.unwrap_or(st.max_payload).min(st.max_payload);
    let mut parser = FrameParser::new(cap);
    let mut events = parser.feed(&body);
    events.extend(parser.finish());
    Json(json!({
        "max_payload": cap,
        "input_len": body.len(),
        "stats": stats_json(&parser),
        "events": events_to_json(&events),
    }))
}

#[derive(Deserialize)]
struct ParseQuery {
    max_payload: Option<usize>,
}

async fn create_session(State(st): State<AppState>) -> Result<Json<Value>, (StatusCode, String)> {
    let mut sessions = st.sessions.lock().unwrap();
    if sessions.len() >= MAX_SESSIONS {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            format!("session limit ({MAX_SESSIONS}) reached"),
        ));
    }
    let n = st
        .session_seq
        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let id = format!(
        "{:x}-{:016x}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
        n
    );
    sessions.insert(id.clone(), FrameParser::new(st.max_payload));
    Ok(Json(json!({"session_id": id, "max_payload": st.max_payload})))
}

fn with_session<R>(
    st: &AppState,
    id: &str,
    f: impl FnOnce(&mut FrameParser) -> R,
) -> Result<R, (StatusCode, String)> {
    let mut sessions = st.sessions.lock().unwrap();
    let parser = sessions
        .get_mut(id)
        .ok_or((StatusCode::NOT_FOUND, "unknown session id".to_string()))?;
    Ok(f(parser))
}

async fn feed_session(
    State(st): State<AppState>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, (StatusCode, String)> {
    let events = with_session(&st, &id, |p| p.feed(&body))?;
    let stats = with_session(&st, &id, |p| stats_json(p))?;
    Ok(Json(json!({
        "session_id": id,
        "events": events_to_json(&events),
        "stats": stats,
    })))
}

async fn finish_session(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let events = with_session(&st, &id, |p| p.finish())?;
    let stats = with_session(&st, &id, |p| stats_json(p))?;
    Ok(Json(json!({
        "session_id": id,
        "events": events_to_json(&events),
        "stats": stats,
    })))
}

async fn drop_session(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<StatusCode, (StatusCode, String)> {
    let mut sessions = st.sessions.lock().unwrap();
    sessions
        .remove(&id)
        .map(|_| StatusCode::NO_CONTENT)
        .ok_or((StatusCode::NOT_FOUND, "unknown session id".to_string()))
}

#[derive(Deserialize)]
struct EncodeReq {
    sequence: u16,
    payload_hex: String,
}

async fn encode(
    State(st): State<AppState>,
    Json(req): Json<EncodeReq>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let payload = hex_decode(&req.payload_hex)
        .map_err(|e| (StatusCode::BAD_REQUEST, format!("bad payload_hex: {e}")))?;
    let frame = encode_frame(req.sequence, &payload, st.max_payload)
        .map_err(|e| (StatusCode::BAD_REQUEST, e.to_string()))?;
    Ok(Json(json!({
        "frame_hex": hex_encode(&frame),
        "frame_len": frame.len(),
    })))
}

#[derive(Deserialize)]
struct SampleQuery {
    format: Option<String>,
}

/// Deterministic demo stream (shared with `examples/make_sample.rs`).
pub fn sample_stream(max_payload: usize) -> Vec<u8> {
    crate::sample::build(max_payload)
}

async fn sample(
    State(st): State<AppState>,
    Query(q): Query<SampleQuery>,
) -> axum::response::Response {
    let data = sample_stream(st.max_payload);
    if q.format.as_deref() == Some("hex") {
        return Json(json!({
            "description": "noise | f1 ping | f2 payload-with-magic | noise | f3 bad-crc | f4 | oversized-len(5000) | f5 | truncated f6",
            "len": data.len(),
            "hex": hex_encode(&data),
        }))
        .into_response();
    }
    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        "application/octet-stream".parse().unwrap(),
    );
    headers.insert(
        header::CONTENT_DISPOSITION,
        "attachment; filename=\"sample_stream.bin\"".parse().unwrap(),
    );
    (headers, data).into_response()
}

// ---------- tiny local hex helpers (no extra crate) ----------

fn hex_encode(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}

fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if !s.len().is_multiple_of(2) {
        return Err("odd number of hex digits".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = (bytes[i] as char).to_digit(16).ok_or("non-hex digit")?;
        let lo = (bytes[i + 1] as char)
            .to_digit(16)
            .ok_or("non-hex digit")?;
        out.push((hi * 16 + lo) as u8);
        i += 2;
    }
    Ok(out)
}
