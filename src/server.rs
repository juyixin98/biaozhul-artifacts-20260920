//! Axum HTTP replay service.
//!
//! Each *session* owns an independent [`Parser`] (independent byte stream,
//! independent sequence expectation). Clients feed arbitrary byte slices to a
//! session and read back the events/frames the incremental parser produced.
//!
//! | Method | Path | Purpose |
//! |---|---|---|
//! | `GET`  | `/healthz` | liveness + configuration |
//! | `GET`  | `/v1/sessions` | list sessions |
//! | `POST` | `/v1/sessions/{id}/feed` | append raw bytes (`application/octet-stream`) |
//! | `GET`  | `/v1/sessions/{id}/events?since={index}` | event log (long-poll-free polling) |
//! | `GET`  | `/v1/sessions/{id}/frames` | accepted frames only, hex-encoded payloads |
//! | `POST` | `/v1/sessions/{id}/reset` | clear buffer + event/frame log + sequence state |
//! | `POST` | `/v1/encode` | encode `{seq, payload_hex}` into a real frame |
//!
//! Every per-session log is capped (oldest entries evicted) and the number of
//! sessions is capped, so the service itself stays memory-bounded just like the
//! parser. Request bodies are limited to one maximum-sized frame.

// Handlers idiomatically return `Result<_, Response>`; boxing the error would
// only add indirection to a cold path.
#![allow(clippy::result_large_err)]

use std::collections::HashMap;
use std::sync::Mutex;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::parser::{Event, Parser, HEADER_LEN, TRAILER_LEN};

/// Maximum number of concurrent sessions (oldest are NOT evicted; creation of
/// further sessions returns 409 so a client cannot force unbounded growth).
pub const DEFAULT_MAX_SESSIONS: usize = 64;
/// Per-session log cap (events and frames each).
pub const DEFAULT_LOG_CAP: usize = 4096;
/// Hard cap on a single session id length.
const MAX_SESSION_ID_LEN: usize = 128;

#[derive(Clone)]
pub struct AppState {
    inner: std::sync::Arc<Mutex<StateInner>>,
    max_payload: usize,
    max_sessions: usize,
    log_cap: usize,
}

struct StateInner {
    sessions: HashMap<String, Session>,
    /// Insertion order for diagnostics.
    order: Vec<String>,
}

struct Session {
    parser: Parser,
    events: Vec<Value>,
    /// Total events ever produced on this session (even evicted ones), so
    /// `since` indexes remain meaningful after the log cap is reached.
    event_counter: u64,
    frame_counter: u64,
}

impl Session {
    fn new(max_payload: usize) -> Self {
        Self {
            parser: Parser::with_max_payload(max_payload),
            events: Vec::new(),
            event_counter: 0,
            frame_counter: 0,
        }
    }

    fn push_event(&mut self, value: Value, log_cap: usize) {
        let idx = self.event_counter;
        self.event_counter += 1;
        let mut v = value;
        v.as_object_mut()
            .expect("event is an object")
            .insert("index".into(), json!(idx));
        self.events.push(v);
        if self.events.len() > log_cap {
            let remove = self.events.len() - log_cap;
            self.events.drain(..remove);
        }
    }
}

/// Build the application. `max_payload` mirrors [`Parser::with_max_payload`].
pub fn app(max_payload: usize) -> Router {
    app_with_limits(max_payload, DEFAULT_MAX_SESSIONS, DEFAULT_LOG_CAP)
}

pub fn app_with_limits(max_payload: usize, max_sessions: usize, log_cap: usize) -> Router {
    let max_payload = max_payload.min(u16::MAX as usize);
    let hard_bound = HEADER_LEN + max_payload + TRAILER_LEN;
    let state = AppState {
        inner: std::sync::Arc::new(Mutex::new(StateInner {
            sessions: HashMap::new(),
            order: Vec::new(),
        })),
        max_payload,
        max_sessions,
        log_cap,
    };

    Router::new()
        .route("/", get(root))
        .route("/healthz", get(healthz))
        .route("/v1/sessions", get(list_sessions))
        .route("/v1/sessions/{id}/feed", post(feed))
        .route("/v1/sessions/{id}/events", get(events))
        .route("/v1/sessions/{id}/frames", get(frames))
        .route("/v1/sessions/{id}/reset", post(reset))
        .route("/v1/encode", post(encode))
        .with_state(state)
        // First line of defense against absurd uploads. Sized to also fit a
        // hex-encoded JSON encode request (2 hex chars per payload byte); the
        // strict per-frame bound is enforced by the parser itself, which
        // answers oversized feed chunks with a 413 JSON error before copying.
        .layer(DefaultBodyLimit::max((2 * max_payload + 256).max(hard_bound)))
}

async fn root(State(st): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "service": "serialframe-replay",
        "max_payload": st.max_payload,
        "hard_bound_bytes": HEADER_LEN + st.max_payload + TRAILER_LEN,
        "max_sessions": st.max_sessions,
        "endpoints": [
            "GET  /healthz",
            "GET  /v1/sessions",
            "POST /v1/sessions/{id}/feed",
            "GET  /v1/sessions/{id}/events?since=0",
            "GET  /v1/sessions/{id}/frames",
            "POST /v1/sessions/{id}/reset",
            "POST /v1/encode",
        ],
    }))
}

async fn healthz(State(st): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "max_payload": st.max_payload,
        "max_sessions": st.max_sessions,
        "log_cap": st.log_cap,
    }))
}

#[derive(Serialize)]
struct SessionInfo {
    id: String,
    buffered: usize,
    events_total: u64,
    frames_total: u64,
    expected_seq: Option<u16>,
}

async fn list_sessions(State(st): State<AppState>) -> impl IntoResponse {
    let guard = st.inner.lock().unwrap();
    let infos: Vec<SessionInfo> = guard
        .order
        .iter()
        .filter_map(|id| {
            guard.sessions.get(id).map(|s| SessionInfo {
                id: id.clone(),
                buffered: s.parser.buffered_len(),
                events_total: s.event_counter,
                frames_total: s.frame_counter,
                expected_seq: s.parser.expected_seq(),
            })
        })
        .collect();
    Json(json!({ "sessions": infos }))
}

fn event_to_json(ev: &Event) -> Value {
    match ev {
        Event::Frame(f) => json!({
            "type": "frame",
            "seq": f.seq,
            "payload_len": f.payload.len(),
            "payload_hex": hex_encode(&f.payload),
            "payload_utf8": String::from_utf8_lossy(&f.payload),
        }),
        Event::Noise { bytes } => json!({ "type": "noise", "bytes": bytes }),
        Event::Oversize { declared, max } => json!({
            "type": "oversize",
            "declared": declared,
            "max": max,
        }),
        Event::CrcMismatch {
            seq,
            declared_len,
            expected_seq,
        } => json!({
            "type": "crc_mismatch",
            "seq": seq,
            "declared_len": declared_len,
            "expected_seq": expected_seq,
        }),
        Event::Gap { from, to, count } => json!({
            "type": "gap",
            "from": from,
            "to": to,
            "count": count,
        }),
        Event::SeqRewind {
            received,
            expected_seq,
        } => json!({
            "type": "seq_rewind",
            "received": received,
            "expected_seq": expected_seq,
        }),
    }
}

/// Validate/lookup-or-create a session, returning an HTTP error on bad id / cap.
fn with_session<R>(
    st: &AppState,
    id: &str,
    create: bool,
    f: impl FnOnce(&mut Session) -> R,
) -> Result<R, Response> {
    if id.is_empty() || id.len() > MAX_SESSION_ID_LEN || !id.chars().all(is_safe_id_char) {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "invalid session id (1..128 url-safe chars)"}))
                .into_response(),
        )
            .into_response());
    }
    let mut guard = st.inner.lock().unwrap();
    if !guard.sessions.contains_key(id) {
        if !create {
            return Err((
                StatusCode::NOT_FOUND,
                Json(json!({ "error": "session not found", "id": id })).into_response(),
            )
                .into_response());
        }
        if guard.sessions.len() >= st.max_sessions {
            return Err((
                StatusCode::CONFLICT,
                Json(json!({
                    "error": "session limit reached",
                    "max_sessions": st.max_sessions,
                }))
                .into_response(),
            )
                .into_response());
        }
        guard.sessions.insert(id.to_string(), Session::new(st.max_payload));
        guard.order.push(id.to_string());
    }
    let session = guard.sessions.get_mut(id).unwrap();
    Ok(f(session))
}

fn is_safe_id_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '~')
}

async fn feed(
    State(st): State<AppState>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, Response> {
    with_session(&st, &id, true, |s| {
        match s.parser.feed(&body) {
            Ok(events) => {
                let mut produced = Vec::with_capacity(events.len());
                for ev in &events {
                    if let Event::Frame(fr) = ev {
                        s.frame_counter += 1;
                        let _ = fr;
                    }
                    let v = event_to_json(ev);
                    s.push_event(v.clone(), st.log_cap);
                    produced.push(v);
                }
                Ok(Json(json!({
                    "session": id,
                    "accepted_bytes": body.len(),
                    "buffered": s.parser.buffered_len(),
                    "events_total": s.event_counter,
                    "events": produced,
                })))
            }
            Err(e) => Err((
                StatusCode::PAYLOAD_TOO_LARGE,
                Json(json!({ "error": e.to_string() })),
            )
                .into_response()),
        }
    })?
}

#[derive(Deserialize)]
struct SinceQuery {
    since: Option<u64>,
}

async fn events(
    State(st): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<SinceQuery>,
) -> Result<Json<Value>, Response> {
    with_session(&st, &id, false, |s| {
        let since = q.since.unwrap_or(0);
        // Events with index >= since. Log may have evicted the oldest entries.
        let oldest = s.event_counter - s.events.len() as u64;
        let partial = since < oldest;
        let start = if since >= oldest {
            (since - oldest) as usize
        } else {
            0
        };
        let slice: Vec<Value> = s
            .events
            .iter()
            .skip(start)
            .cloned()
            .collect();
        Json(json!({
            "session": id,
            "since": since,
            "history_partial": partial,
            "next_index": s.event_counter,
            "events": slice,
        }))
    })
}

async fn frames(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    with_session(&st, &id, false, |s| {
        let out: Vec<Value> = s
            .events
            .iter()
            .filter(|v| v.get("type").and_then(|t| t.as_str()) == Some("frame"))
            .cloned()
            .collect();
        Json(json!({
            "session": id,
            "frames_total": s.frame_counter,
            "frames": out,
        }))
    })
}

async fn reset(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    with_session(&st, &id, false, |s| {
        s.parser.reset_all();
        s.events.clear();
        s.event_counter = 0;
        s.frame_counter = 0;
        Json(json!({ "session": id, "status": "reset" }))
    })
}

#[derive(Deserialize)]
struct EncodeReq {
    seq: u16,
    /// Hex-encoded payload (preferred).
    payload_hex: Option<String>,
    /// Optional raw UTF-8 payload; ignored if payload_hex is present.
    payload_utf8: Option<String>,
}

async fn encode(
    State(st): State<AppState>,
    Json(req): Json<EncodeReq>,
) -> Result<Json<Value>, Response> {
    let payload = if let Some(hx) = req.payload_hex {
        match hex_decode(&hx) {
            Ok(p) => p,
            Err(e) => {
                return Err((
                    StatusCode::BAD_REQUEST,
                    Json(json!({ "error": e })),
                )
                    .into_response());
            }
        }
    } else if let Some(u) = req.payload_utf8 {
        u.into_bytes()
    } else {
        Vec::new()
    };

    if payload.len() > st.max_payload {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            Json(json!({
                "error": format!(
                    "payload {} bytes exceeds server max_payload {}",
                    payload.len(),
                    st.max_payload
                ),
            })),
        )
            .into_response());
    }

    match crate::parser::encode_frame(req.seq, &payload) {
        Ok(wire) => Ok(Json(json!({
            "seq": req.seq,
            "payload_len": payload.len(),
            "bytes_hex": hex_encode(&wire),
        }))),
        Err(e) => Err((StatusCode::BAD_REQUEST, Json(json!({ "error": e.to_string() })))
            .into_response()),
    }
}

// ---------------------------------------------------------------------------
// Hex helpers (real, dependency-free)
// ---------------------------------------------------------------------------

pub fn hex_encode(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}

pub fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    let s = s.trim();
    if !s.len().is_multiple_of(2) {
        return Err("hex string has odd length".to_string());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_nibble(bytes[i])?;
        let lo = hex_nibble(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        other => Err(format!("invalid hex digit: {other:#x}")),
    }
}
