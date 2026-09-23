//! 目标链桩（本地模拟目标链，但所有判定都是真实执行的）：
//! - HMAC-SHA256 请求验签（密钥与中继共享，常量时间比较）；
//! - 按通道校验 nonce 严格连续（非幂等重放若跳号 -> 422，对中继是致命错误）；
//! - 按提交 ID 幂等：同 ID 重放绝不重复入账，返回同一笔；
//! - GET /receipts 按提交 ID 查询结果，未知 ID 返回 404（调用方不得把未知当失败）；
//! - 故障注入：drop_first（响应丢失但已入账）、fail_attempts（前 n 次 503）、
//!   fatal/fatal_once（422 永久/一次性拒绝）、slow_ok（先 accepted 延迟 confirmed）。

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

use crate::crypto;

#[derive(Clone)]
struct StubState {
    inner: Arc<Mutex<StubInner>>,
    secret: Arc<String>,
    default_mode: Arc<String>,
}

struct StubInner {
    entries: HashMap<String, Entry>,
    // 每个提交 ID 已观察到的 submit 次数（驱动 fail_attempts / fatal_once / drop_first）
    seen: HashMap<String, u32>,
    // 每通道上一个已入账 nonce；初始期待 0
    channel_nonce: HashMap<String, i64>,
}

#[derive(Debug, Clone)]
struct Entry {
    id: String,
    channel_id: String,
    nonce: i64,
    tx_hash: String,
    #[allow(dead_code)]
    status: String, // accepted | confirmed（当前以 confirm_after 推算，字段留作持久化语义）
    stored_at: Instant,
    confirm_after: Option<Duration>,
}

#[derive(Deserialize)]
struct SubmitBody {
    id: String,
    channel_id: String,
    nonce: i64,
    fence: i32,
    relay_id: String,
    payload: Value,
    /// 单条故障注入指令，覆盖桩全局模式（测试使用）
    sim: Option<String>,
}

pub fn router(secret: String, default_mode: String) -> Router {
    let state = StubState {
        inner: Arc::new(Mutex::new(StubInner {
            entries: HashMap::new(),
            seen: HashMap::new(),
            channel_nonce: HashMap::new(),
        })),
        secret: Arc::new(secret),
        default_mode: Arc::new(default_mode),
    };
    Router::new()
        .route("/healthz", get(|| async { Json(json!({"ok": true, "service": "target-stub"})) }))
        .route("/submit", axum::routing::post(submit))
        .route("/receipts", get(receipts))
        .route("/admin/entries", axum::routing::get(admin_entries))
        .with_state(state)
}

fn err(status: StatusCode, code: &str, msg: &str) -> Response {
    (status, Json(json!({"error": code, "message": msg}))).into_response()
}

fn bearer(headers: &HeaderMap) -> Option<String> {
    headers
        .get("X-Signature")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

/// 模拟模式：parse 成 (种类, 参数)。
#[derive(Debug, PartialEq)]
enum Mode {
    Normal,
    DropFirst(u64),
    FailAttempts(u32),
    Fatal,
    FatalOnce,
    SlowOk(u64),
}

fn parse_mode(s: &str) -> Mode {
    let s = s.trim();
    if let Some(n) = s.strip_prefix("drop_first:") {
        return Mode::DropFirst(n.parse().unwrap_or(5000));
    }
    if let Some(n) = s.strip_prefix("fail_attempts:") {
        return Mode::FailAttempts(n.parse().unwrap_or(1));
    }
    if let Some(n) = s.strip_prefix("slow_ok:") {
        return Mode::SlowOk(n.parse().unwrap_or(2000));
    }
    match s {
        "fatal" => Mode::Fatal,
        "fatal_once" => Mode::FatalOnce,
        _ => Mode::Normal,
    }
}

async fn submit(
    State(st): State<StubState>,
    headers: HeaderMap,
    Json(body): Json<SubmitBody>,
) -> Response {
    // 1) 验签（真实 HMAC，签名覆盖 relay/fence/nonce/channel/id/payload）
    let sig = match bearer(&headers) {
        Some(s) => s,
        None => return err(StatusCode::UNAUTHORIZED, "missing_signature", "X-Signature required"),
    };
    let canonical = crypto::submit_canonical(
        &body.relay_id,
        body.fence,
        body.nonce,
        &body.channel_id,
        &body.id,
        &body.payload,
    );
    if !crypto::verify(&st.secret, &canonical, &sig) {
        return err(StatusCode::UNAUTHORIZED, "bad_signature", "HMAC verification failed");
    }

    let mode = match &body.sim {
        Some(m) => parse_mode(m),
        None => parse_mode(&st.default_mode),
    };

    let mut inner = st.inner.lock().await;
    // 幂等：同 ID 重放，直接返回既有笔（绝不重复分配 nonce）
    if let Some(existing) = inner.entries.get(&body.id).cloned() {
        let visible = current_status(&existing);
        return Json(json!({
            "id": existing.id, "status": visible,
            "tx_hash": existing.tx_hash, "replay": true,
        }))
        .into_response();
    }

    let attempt = {
        let n = inner.seen.entry(body.id.clone()).or_insert(0);
        *n += 1;
        *n
    };

    // 2) nonce 连续性校验（仅对真正新入账的提交）
    let expected = inner.channel_nonce.get(&body.channel_id).copied().unwrap_or(-1) + 1;
    if body.nonce != expected {
        return err(
            StatusCode::UNPROCESSABLE_ENTITY,
            "nonce_gap",
            &format!("expected nonce {expected}, got {}", body.nonce),
        );
    }

    // 3) 故障注入（在入账之前判定，保证失败不产生缺口）
    match mode {
        Mode::FailAttempts(n) if attempt <= n => {
            return err(
                StatusCode::SERVICE_UNAVAILABLE,
                "transient",
                &format!("simulated transient failure {attempt}/{n}"),
            );
        }
        Mode::Fatal => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "rejected",
                "simulated permanent rejection",
            );
        }
        Mode::FatalOnce if attempt == 1 => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "rejected",
                "simulated one-shot permanent rejection",
            );
        }
        _ => {}
    }

    // 4) 真实入账：确定性交易哈希
    let (status, confirm_after) = match mode {
        Mode::SlowOk(ms) => ("accepted", Some(Duration::from_millis(ms))),
        _ => ("confirmed", None),
    };
    let entry = Entry {
        tx_hash: crypto::tx_hash(&body.channel_id, body.nonce, &body.id, &body.payload),
        id: body.id.clone(),
        channel_id: body.channel_id.clone(),
        nonce: body.nonce,
        status: status.to_string(),
        stored_at: Instant::now(),
        confirm_after,
    };
    inner
        .channel_nonce
        .insert(body.channel_id.clone(), body.nonce);
    inner.entries.insert(body.id.clone(), entry.clone());
    drop(inner);

    // drop_first：已入账，但响应延迟到超过中继的请求超时之后才返回（模拟响应丢包）
    if let Mode::DropFirst(ms) = mode {
        if attempt == 1 {
            tokio::time::sleep(Duration::from_millis(ms)).await;
        }
    }

    Json(json!({"id": entry.id, "status": status, "tx_hash": entry.tx_hash})).into_response()
}

fn current_status(e: &Entry) -> String {
    match e.confirm_after {
        Some(d) if e.stored_at.elapsed() < d => "accepted".to_string(),
        _ => "confirmed".to_string(),
    }
}

#[derive(Deserialize)]
struct ReceiptQuery {
    id: String,
    ts: i64,
}

async fn receipts(
    State(st): State<StubState>,
    headers: HeaderMap,
    Query(q): Query<ReceiptQuery>,
) -> Response {
    let Some(sig) = bearer(&headers) else {
        return err(StatusCode::UNAUTHORIZED, "missing_signature", "X-Signature required");
    };
    let canonical = crypto::receipt_canonical(&q.id, q.ts);
    if !crypto::verify(&st.secret, &canonical, &sig) {
        return err(StatusCode::UNAUTHORIZED, "bad_signature", "HMAC verification failed");
    }
    let now_unix = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    if (now_unix - q.ts).abs() > 300 {
        return err(StatusCode::UNAUTHORIZED, "stale_ts", "timestamp skew too large");
    }

    let inner = st.inner.lock().await;
    match inner.entries.get(&q.id) {
        Some(e) => {
            let status = current_status(e);
            let mut body = json!({"id": e.id, "status": status});
            if status == "confirmed" {
                body["tx_hash"] = json!(e.tx_hash);
            }
            Json(body).into_response()
        }
        // 关键语义：未知 ID 是 404，而不是“失败”
        None => err(StatusCode::NOT_FOUND, "unknown_commit", "no such commit id"),
    }
}

async fn admin_entries(State(st): State<StubState>) -> Json<Value> {
    let inner = st.inner.lock().await;
    let entries: Vec<Value> = inner
        .entries
        .values()
        .map(|e| {
            json!({"id": e.id, "channel_id": e.channel_id, "nonce": e.nonce,
                   "status": current_status(e), "tx_hash": e.tx_hash})
        })
        .collect();
    Json(json!({"count": entries.len(), "entries": entries}))
}
