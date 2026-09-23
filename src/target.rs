//! 本地模拟的"目标链"桩。
//!
//! 真实语义（不是假返回）：
//! - 提交按 `submission_id` 幂等：重复提交返回同一条链上记录（`replayed=true`），
//!   绝不产生第二笔交易；
//! - 交易 ID 是对 `submission_id|channel|nonce|payload` 真实计算 SHA-256 得到的；
//! - 支持结果查询 `GET /result/:id`，未知 ID 返回 404 —— 中继必须据此区分
//!   "未知"与"失败"，未知时只能重试同一个 submission_id；
//! - 故障注入：延迟响应（模拟提交成功但响应丢失）、确定性失败（模拟链上拒绝）。

use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize)]
pub struct Observation {
    pub fencing_token: i64,
    pub relay_id: String,
    pub replayed: bool,
    pub at: DateTime<Utc>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Record {
    pub submission_id: Uuid,
    pub channel_id: String,
    pub nonce: i64,
    pub status: String,
    pub tx_id: Option<String>,
    pub block_hash: Option<String>,
    pub payload_hash: String,
    pub error: Option<String>,
    pub observed: Vec<Observation>,
    pub created_at: DateTime<Utc>,
    pub confirmed_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Rule {
    /// 前 N 次提交：先把交易真实落盘，再拖住响应（模拟响应在网络里丢失，查询能命中）。
    #[serde(default)]
    pub drop_responses: u32,
    /// 拖响应的秒数（应大于中继的提交超时）。
    #[serde(default)]
    pub sleep_secs: u64,
    /// 落盘前先拖住这么多秒（模拟链上接受慢；此期间提交是 404，
    /// 中继必须用同一个 submission_id 重发而不是造新 ID）。
    #[serde(default)]
    pub commit_delay_secs: u64,
    /// 结果查询拖住这么多秒后才返回（模拟网络对 GET 也丢包）。
    #[serde(default)]
    pub query_delay_secs: u64,
    /// 链上确定性拒绝（不是超时，中继可据此上报失败）。
    #[serde(default)]
    pub always_fail: bool,
}

impl Default for Rule {
    fn default() -> Self {
        Self {
            drop_responses: 0,
            sleep_secs: 0,
            commit_delay_secs: 0,
            query_delay_secs: 0,
            always_fail: false,
        }
    }
}

#[derive(Default)]
struct Inner {
    records: HashMap<Uuid, Record>,
    rules: HashMap<Uuid, Rule>,
    /// 已接受提交、正在 commit_delay 窗口内、记录尚未落盘的 ID。
    /// 并发重复提交命中它时返回"未知"，而不是产生第二笔。
    inflight: std::collections::HashSet<Uuid>,
}

#[derive(Clone)]
pub struct StubState {
    inner: Arc<Mutex<Inner>>,
}

impl StubState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner::default())),
        }
    }

    pub async fn set_rule(&self, id: Uuid, rule: Rule) {
        self.inner.lock().await.rules.insert(id, rule);
    }

    pub async fn clear_rule(&self, id: Uuid) {
        self.inner.lock().await.rules.remove(&id);
    }

    pub async fn record(&self, id: Uuid) -> Option<Record> {
        self.inner.lock().await.records.get(&id).cloned()
    }

    pub async fn records(&self) -> Vec<Record> {
        let mut v: Vec<_> = self.inner.lock().await.records.values().cloned().collect();
        v.sort_by_key(|r| (r.channel_id.clone(), r.nonce));
        v
    }
}

/// 真实 SHA-256，交易 ID 形如 0x…（模拟链上交易哈希）。
pub fn payload_hash(submission_id: Uuid, channel_id: &str, nonce: i64, payload: &Value) -> String {
    let mut h = Sha256::new();
    h.update(submission_id.as_bytes());
    h.update(b"|");
    h.update(channel_id.as_bytes());
    h.update(b"|");
    h.update(nonce.to_be_bytes());
    h.update(b"|");
    h.update(canonical_json(payload));
    hex::encode(h.finalize())
}

/// 稳定序列化：紧凑、无空格。同一 Value 永远得到同一字节串。
fn canonical_json(v: &Value) -> Vec<u8> {
    serde_json::to_vec(v).expect("Value always serializes")
}

fn tx_id(hash: &str) -> String {
    format!("0x{hash}")
}

#[derive(Deserialize, Clone)]
pub struct SubmitBody {
    pub submission_id: Uuid,
    pub channel_id: String,
    pub nonce: i64,
    pub fencing_token: i64,
    pub relay_id: String,
    pub payload: Value,
}

#[derive(Serialize)]
struct SubmitResponse<'a> {
    ok: bool,
    replayed: bool,
    tx_id: Option<&'a str>,
    block_hash: Option<&'a str>,
    payload_hash: &'a str,
    error: Option<&'a str>,
}

/// submit 的三种出口（都在锁外再做延迟/响应）。
enum SubmitOutcome {
    Reply {
        record: Record,
        replayed: bool,
        sleep_secs: Option<u64>,
    },
    /// 已接受但正在 commit_delay 窗口：调用方延迟后落盘并返回成功。
    CommitAfter { rule: Rule },
    /// 同 ID 已有一笔提交在 commit 窗口里：结果未知，立刻 503 让中继去查询/重发。
    InFlight,
}

async fn submit(
    State(state): State<StubState>,
    Json(body): Json<SubmitBody>,
) -> (StatusCode, Json<Value>) {
    let outcome = {
        let mut g = state.inner.lock().await;

        if let Some(existing) = g.records.get(&body.submission_id) {
            // 故障注入规则已解除 + 旧记录是 failed：模拟运维排除故障后，用同一个稳定
            // submission_id 重新上链成功（幂等键不变，不产生新 ID）。
            let rule_still_failing = g
                .rules
                .get(&body.submission_id)
                .map(|r| r.always_fail)
                .unwrap_or(false);
            if existing.status == "failed" && !rule_still_failing {
                let rebuilt = build_record(&body, &g.rules);
                g.records.insert(body.submission_id, rebuilt.clone());
                SubmitOutcome::Reply {
                    record: rebuilt,
                    replayed: false,
                    sleep_secs: None,
                }
            } else {
                let mut record = existing.clone();
                record.observed.push(Observation {
                    fencing_token: body.fencing_token,
                    relay_id: body.relay_id.clone(),
                    replayed: true,
                    at: Utc::now(),
                });
                g.records.insert(body.submission_id, record.clone());
                // 重放总是立刻返回（查询-重发协议靠它恢复），不参与 drop_responses 计数。
                SubmitOutcome::Reply {
                    record,
                    replayed: true,
                    sleep_secs: None,
                }
            }
        } else if g.inflight.contains(&body.submission_id) {
            SubmitOutcome::InFlight
        } else {
            let rule = g.rules.get(&body.submission_id).cloned().unwrap_or_default();
            if rule.commit_delay_secs > 0 {
                g.inflight.insert(body.submission_id);
                SubmitOutcome::CommitAfter { rule }
            } else {
                let record = build_record(&body, &g.rules);
                g.records.insert(body.submission_id, record.clone());
                // 仅对"首次提交且配置了丢响应"计数。
                let sleep_secs = match g.rules.get_mut(&body.submission_id) {
                    Some(r) if r.drop_responses > 0 => {
                        r.drop_responses -= 1;
                        Some(r.sleep_secs)
                    }
                    _ => None,
                };
                SubmitOutcome::Reply {
                    record,
                    replayed: false,
                    sleep_secs,
                }
            }
        }
    };

    match outcome {
        SubmitOutcome::InFlight => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "ok": false,
                "error": "submission in flight; query /result/{id}",
            })),
        ),
        SubmitOutcome::CommitAfter { rule } => {
            // 链上已接受、落盘慢：在**独立任务**里延迟落盘。
            // 客户端超时断开不影响上链（真实链的 mempool 语义），
            // handler 立即返回 202，调用方应通过查询/重发跟进。
            let state2 = state.clone();
            let body2 = body.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(rule.commit_delay_secs)).await;
                let mut g = state2.inner.lock().await;
                g.inflight.remove(&body2.submission_id);
                if !g.records.contains_key(&body2.submission_id) {
                    let r = build_record(&body2, &g.rules);
                    g.records.insert(body2.submission_id, r);
                }
            });
            (
                StatusCode::ACCEPTED,
                Json(serde_json::json!({
                    "ok": false,
                    "error": "accepted; committing asynchronously",
                })),
            )
        }
        SubmitOutcome::Reply {
            record,
            replayed,
            sleep_secs,
        } => {
            // 交易已落盘，但响应在网络里"丢失"——睡在锁外，不影响查询接口。
            if let Some(secs) = sleep_secs {
                tokio::time::sleep(Duration::from_secs(secs.max(1))).await;
            }
            reply_with(&record, replayed, None)
        }
    }
}

fn build_record(body: &SubmitBody, rules: &HashMap<Uuid, Rule>) -> Record {
    let hash = payload_hash(body.submission_id, &body.channel_id, body.nonce, &body.payload);
    let now = Utc::now();
    let rule = rules.get(&body.submission_id);
    let (status, tx, block_hash, error, confirmed_at) = match rule {
        Some(r) if r.always_fail => (
            "failed",
            None,
            None,
            Some("chain rejected: deterministic rule".to_string()),
            None,
        ),
        _ => {
            let tx = tx_id(&hash);
            let block_hash =
                format!("0x{}", &hex::encode(Sha256::digest(tx.as_bytes()))[..40]);
            ("confirmed", Some(tx), Some(block_hash), None, Some(now))
        }
    };
    Record {
        submission_id: body.submission_id,
        channel_id: body.channel_id.clone(),
        nonce: body.nonce,
        status: status.to_string(),
        tx_id: tx,
        block_hash,
        payload_hash: hash,
        error,
        observed: vec![Observation {
            fencing_token: body.fencing_token,
            relay_id: body.relay_id.clone(),
            replayed: false,
            at: now,
        }],
        created_at: now,
        confirmed_at,
    }
}

fn reply_with(record: &Record, replayed: bool, _sleep: Option<u64>) -> (StatusCode, Json<Value>) {
    let ok = record.status == "confirmed";
    let resp = SubmitResponse {
        ok,
        replayed,
        tx_id: record.tx_id.as_deref(),
        block_hash: record.block_hash.as_deref(),
        payload_hash: &record.payload_hash,
        error: record.error.as_deref(),
    };
    let status = if ok {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    (status, Json(serde_json::to_value(resp).unwrap()))
}

async fn get_result(
    State(state): State<StubState>,
    AxumPath(id): AxumPath<Uuid>,
) -> Result<Json<Record>, StatusCode> {
    let delay = state
        .inner
        .lock()
        .await
        .rules
        .get(&id)
        .map(|r| r.query_delay_secs)
        .unwrap_or(0);
    if delay > 0 {
        tokio::time::sleep(Duration::from_secs(delay)).await;
    }
    state.record(id).await.map(Json).ok_or(StatusCode::NOT_FOUND)
}

async fn list_records(State(state): State<StubState>) -> Json<Vec<Record>> {
    Json(state.records().await)
}

async fn put_rule(
    State(state): State<StubState>,
    AxumPath(id): AxumPath<Uuid>,
    Json(rule): Json<Rule>,
) -> Json<Value> {
    state.set_rule(id, rule.clone()).await;
    Json(serde_json::json!({"ok": true, "submission_id": id, "rule": rule}))
}

async fn delete_rule(State(state): State<StubState>, AxumPath(id): AxumPath<Uuid>) -> Json<Value> {
    state.clear_rule(id).await;
    Json(serde_json::json!({"ok": true, "submission_id": id}))
}

/// 桩的路由（自带全新状态）。bin 使用。
pub fn router() -> Router {
    shared_router(StubState::new())
}

/// 注入预置状态的路由。集成测试使用。
pub fn shared_router(state: StubState) -> Router {
    Router::new()
        .route("/submit", post(submit))
        .route("/result/{id}", get(get_result))
        .route("/records", get(list_records))
        .route(
            "/admin/rules/{id}",
            axum::routing::put(put_rule).delete(delete_rule),
        )
        .route("/healthz", get(|| async { "ok" }))
        .with_state(state)
}
