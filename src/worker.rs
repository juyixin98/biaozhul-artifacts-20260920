//! 中继 worker：从协调器领取提交 -> 持 fence 调目标桩 -> 回执写回协调器。
//!
//! 关键语义（全部真实执行）：
//! 1. 每次领取拿到的 fence 是单调递增的代次令牌；写回任何结果都必须携带它，
//!    协调器拒绝旧代次（409），worker 立即放弃该条。
//! 2. 对目标的网络超时/5xx：先 GET /receipts 按稳定提交 ID 查询，
//!    确认已入账才回报成功；查不到只按“可重试”处理，绝不生成新提交 ID。
//! 3. 心跳维持租约；一旦心跳得到 409（租约已被新一代中继拿走），立刻停止，
//!    旧代次的迟到回执即使到达也无法覆盖新代次状态。

use crate::api::{ClaimResp, InternalClient};
use crate::crypto;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub struct Config {
    pub relay_id: String,
    pub coordinator: String,
    pub target: String,
    pub secret: String,
    pub lease_secs: i32,
    /// 测试钩子：领取后立即“卡死”，不再心跳、不完成（由外部 SIGKILL 杀进程）
    pub stall_after_claim: bool,
    /// 连续空闲这么多秒后自动退出（测试用，避免进程跨用例残留）；None 永不退出
    pub exit_if_idle_secs: Option<u64>,
}

pub async fn run(cfg: Config, shutdown: Arc<AtomicBool>) -> anyhow::Result<()> {
    let coord = InternalClient::new(cfg.coordinator.clone());
    let target = TargetClient::new(cfg.target.clone(), cfg.secret.clone());
    let poll_idle = Duration::from_millis(300);
    let mut idle_since = std::time::Instant::now();

    tracing::info!(relay = %cfg.relay_id, "relay worker started");

    while !shutdown.load(Ordering::Relaxed) {
        let claimed = match coord.claim(&cfg.relay_id).await {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("claim failed: {e:#}; backing off");
                sleep_or_stop(Duration::from_secs(1), &shutdown).await;
                continue;
            }
        };
        let Some(claim) = claimed else {
            if let Some(max_idle) = cfg.exit_if_idle_secs {
                if idle_since.elapsed() >= Duration::from_secs(max_idle) {
                    tracing::info!(relay = %cfg.relay_id, "idle for {max_idle}s; exiting");
                    return Ok(());
                }
            }
            sleep_or_stop(poll_idle, &shutdown).await;
            continue;
        };
        idle_since = std::time::Instant::now();

        tracing::info!(
            id = %claim.id, channel = %claim.channel_id, nonce = claim.nonce,
            fence = claim.fence, "claimed"
        );

        if cfg.stall_after_claim {
            tracing::warn!(id = %claim.id, "STALL_AFTER_CLAIM: hanging with lease, no heartbeats");
            // 模拟崩溃前卡住：不退出（等待被 SIGKILL）、不续租
            while !shutdown.load(Ordering::Relaxed) {
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            return Ok(());
        }

        process_one(&cfg, &coord, &target, shutdown.clone(), claim).await;
    }
    tracing::info!(relay = %cfg.relay_id, "relay worker stopped");
    Ok(())
}

async fn sleep_or_stop(d: Duration, stop: &AtomicBool) {
    let ticks = (d.as_millis() / 100).max(1) as u64;
    for _ in 0..ticks {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// 处理一条领取到的提交。任何阶段发现 fence 失效都立即放弃。
async fn process_one(
    cfg: &Config,
    coord: &InternalClient,
    target: &TargetClient,
    shutdown: Arc<AtomicBool>,
    claim: ClaimResp,
) {
    let stop = Arc::new(AtomicBool::new(false));
    let hb_cfg = cfg.clone_for_heartbeat();
    let hb_claim = claim.clone();
    let hb_stop = stop.clone();
    let hb_shutdown = shutdown.clone();
    let heartbeat = tokio::spawn(async move {
        heartbeat_loop(hb_cfg, hb_claim, hb_stop, hb_shutdown).await;
    });

    let outcome =
        submit_and_settle(cfg, coord, target, shutdown.clone(), stop.clone(), &claim).await;

    // 终止心跳
    stop.store(true, Ordering::Relaxed);
    heartbeat.abort();

    match outcome {
        Outcome::Done => {}
        Outcome::Stale => tracing::warn!(id=%claim.id, fence=claim.fence, "lost lease (stale fence); aborting with no overwrite"),
        Outcome::Fatal(e) => tracing::error!(id=%claim.id, error=%e, "fatal failure reported; channel head blocked"),
        Outcome::Retryable(e) => tracing::warn!(id=%claim.id, error=%e, "retryable failure; lease released for re-claim"),
    }
}

#[derive(Debug)]
enum Outcome {
    Done,
    Stale,
    Fatal(String),
    Retryable(String),
}

impl Config {
    fn clone_for_heartbeat(&self) -> (String, InternalClient, i32) {
        (
            self.relay_id.clone(),
            InternalClient::new(self.coordinator.clone()),
            self.lease_secs,
        )
    }
}

async fn heartbeat_loop(
    (relay_id, coord, lease_secs): (String, InternalClient, i32),
    claim: ClaimResp,
    stop: Arc<AtomicBool>,
    shutdown: Arc<AtomicBool>,
) {
    // 租约剩余约 1/3 时续租
    let interval = Duration::from_secs_f64(lease_secs as f64 / 3.0).max(Duration::from_millis(200));
    let mut ticker = tokio::time::interval(interval);
    ticker.tick().await; // 立即消费首个零时刻 tick
    while !stop.load(Ordering::Relaxed) && !shutdown.load(Ordering::Relaxed) {
        ticker.tick().await;
        if stop.load(Ordering::Relaxed) {
            break;
        }
        match coord.heartbeat(&claim, &relay_id).await {
            Ok(true) => tracing::debug!(id=%claim.id, fence=claim.fence, "heartbeat ok"),
            Ok(false) => {
                tracing::warn!(id=%claim.id, fence=claim.fence,
                    "heartbeat rejected: fence stale, a newer generation owns the lease");
                stop.store(true, Ordering::Relaxed);
            }
            Err(e) => tracing::warn!("heartbeat error: {e:#}"),
        }
    }
}

async fn submit_and_settle(
    cfg: &Config,
    coord: &InternalClient,
    target: &TargetClient,
    shutdown: Arc<AtomicBool>,
    lost: Arc<AtomicBool>,
    claim: &ClaimResp,
) -> Outcome {
    let max_attempts = 5u32;

    for attempt in 1..=max_attempts {
        if shutdown.load(Ordering::Relaxed) {
            return Outcome::Retryable("shutdown".into());
        }
        if lost.load(Ordering::Relaxed) {
            // 心跳已确认本代次失效（更新的代次接管）：立即停止，不再碰目标。
            return Outcome::Stale;
        }

        match target.submit(claim, &cfg.relay_id).await {
            Ok(SubmitResult::Confirmed(tx)) => {
                return settle_confirmed(coord, cfg, claim, &tx, true).await;
            }
            Ok(SubmitResult::Accepted) => {
                return poll_receipts(coord, target, cfg, shutdown, claim).await;
            }
            Err(TargetError::Fatal(code, msg)) => {
                // 422 等：致命错误（nonce 跳号 / 永久拒绝）。不重试。
                let ok = report_or_release(coord, claim, cfg, "fatal", &format!("{code}: {msg}"))
                    .await;
                return if ok {
                    Outcome::Fatal(msg)
                } else {
                    Outcome::Stale
                };
            }
            Err(TargetError::Transient(msg)) => {
                // 超时 / 连接失败 / 5xx / 404 未知提交：
                // 绝不假定失败后重发新 ID——先按同一提交 ID 查询目标。
                tracing::info!(id=%claim.id, attempt,
                    "submit uncertain ({msg}); querying target receipt before any resend");
                match target.receipt(claim, &cfg.relay_id).await {
                    Ok(ReceiptResult::Confirmed(tx)) => {
                        return settle_confirmed(coord, cfg, claim, &tx, true).await;
                    }
                    Ok(ReceiptResult::Accepted) => {
                        return poll_receipts(coord, target, cfg, shutdown, claim).await;
                    }
                    Ok(ReceiptResult::Unknown) => {
                        // 目标确实没有这笔 -> 安全地用同一提交 ID 重发
                        tracing::info!(id=%claim.id, "target has no record; safe to resend same id");
                        let backoff = Duration::from_millis(200 * attempt as u64);
                        sleep_or_stop(backoff, &shutdown).await;
                    }
                    Err(e) => {
                        tracing::warn!(id=%claim.id, "receipt query failed: {e:#}");
                        let backoff = Duration::from_millis(200 * attempt as u64);
                        sleep_or_stop(backoff, &shutdown).await;
                    }
                }
            }
        }
    }

    // 重试用尽：交还租约（retryable），让本/其他中继用同一提交 ID 再领
    let msg = "submit failed after max attempts";
    let ok = report_or_release(coord, claim, cfg, "retryable", msg).await;
    if ok {
        Outcome::Retryable(msg.into())
    } else {
        Outcome::Stale
    }
}

/// 已确认：写 delivered（若尚未）+ complete。任一环 fence 失效即 Stale。
async fn settle_confirmed(
    coord: &InternalClient,
    cfg: &Config,
    claim: &ClaimResp,
    tx_hash: &str,
    mark_delivered: bool,
) -> Outcome {
    if mark_delivered {
        match coord.delivered(claim, &cfg.relay_id).await {
            Ok(true) => {}
            Ok(false) => return Outcome::Stale,
            Err(e) => {
                tracing::warn!(id=%claim.id, "delivered call failed: {e:#}");
            }
        }
    }
    match coord.complete(claim, &cfg.relay_id, tx_hash).await {
        Ok(true) => {
            tracing::info!(id=%claim.id, nonce=claim.nonce, tx=%tx_hash, "confirmed");
            Outcome::Done
        }
        Ok(false) => Outcome::Stale,
        Err(e) => {
            // 协调器瞬时错误：目标已确认，交还租约，下次领取时查询路径可收敛
            tracing::error!(id=%claim.id, "complete call error: {e:#}; releasing as retryable");
            let _ = report_or_release(coord, claim, cfg, "retryable", "complete call failed").await;
            Outcome::Retryable("complete call failed".into())
        }
    }
}

/// 目标已受理但未最终确认：轮询 receipts。
async fn poll_receipts(
    coord: &InternalClient,
    target: &TargetClient,
    cfg: &Config,
    shutdown: Arc<AtomicBool>,
    claim: &ClaimResp,
) -> Outcome {
    // 先确保协调器上记录 delivered（best effort，fence 失效立即停止）
    match coord.delivered(claim, &cfg.relay_id).await {
        Ok(true) => {}
        Ok(false) => return Outcome::Stale,
        Err(e) => tracing::warn!(id=%claim.id, "delivered call failed: {e:#}"),
    }

    let deadline =
        std::time::Instant::now() + Duration::from_secs((cfg.lease_secs as u64).max(30) * 4);
    let mut interval = tokio::time::interval(Duration::from_millis(400));
    interval.tick().await;
    while std::time::Instant::now() < deadline {
        interval.tick().await;
        if shutdown.load(Ordering::Relaxed) {
            return Outcome::Retryable("shutdown".into());
        }
        match target.receipt(claim, &cfg.relay_id).await {
            Ok(ReceiptResult::Confirmed(tx)) => {
                return settle_confirmed(coord, cfg, claim, &tx, false).await
            }
            Ok(ReceiptResult::Accepted) => continue,
            Ok(ReceiptResult::Unknown) => {
                // 已 accepted 的笔不可能变未知；按可重试交还（下代从查询路径恢复）
                let _ = report_or_release(
                    coord,
                    claim,
                    cfg,
                    "retryable",
                    "accepted receipt vanished",
                )
                .await;
                return Outcome::Retryable("accepted receipt vanished".into());
            }
            Err(e) => tracing::warn!("receipt poll error: {e:#}"),
        }
    }
    let _ = report_or_release(coord, claim, cfg, "retryable", "confirmation timeout").await;
    Outcome::Retryable("confirmation timeout".into())
}

/// 向协调器上报；返回 false 表示 fence 已失效（调用方应按 Stale 处理）。
async fn report_or_release(
    coord: &InternalClient,
    claim: &ClaimResp,
    cfg: &Config,
    kind: &str,
    error: &str,
) -> bool {
    match coord.report(claim, &cfg.relay_id, kind, error).await {
        Ok(alive) => alive,
        Err(e) => {
            tracing::error!(id=%claim.id, "report call failed: {e:#}");
            // 尽力一次 retryable 交还（租约也会自然超时，安全）
            false
        }
    }
}

// ---------- 目标桩 HTTP 客户端 ----------

struct TargetClient {
    http: reqwest::Client,
    base: String,
    secret: String,
}

enum SubmitResult {
    Confirmed(String),
    Accepted,
}

enum ReceiptResult {
    Confirmed(String),
    Accepted,
    Unknown,
}

enum TargetError {
    Fatal(String, String),
    Transient(String),
}

impl TargetClient {
    fn new(base: String, secret: String) -> Self {
        Self {
            http: reqwest::Client::builder()
                // 短超时，便于真实触发“提交成功但响应丢失”的恢复路径
                .connect_timeout(Duration::from_secs(2))
                .timeout(Duration::from_secs(2))
                .build()
                .expect("build client"),
            base,
            secret,
        }
    }

    async fn submit(&self, claim: &ClaimResp, relay_id: &str) -> Result<SubmitResult, TargetError> {
        let canonical = crypto::submit_canonical(
            relay_id,
            claim.fence,
            claim.nonce,
            &claim.channel_id,
            &claim.id,
            &claim.payload,
        );
        let sig = crypto::hmac_hex(&self.secret, &canonical);
        // 测试故障注入约定：payload._sim 透传给桩（正常流量不含此字段）
        let mut body = json!({
            "id": claim.id,
            "channel_id": claim.channel_id,
            "nonce": claim.nonce,
            "fence": claim.fence,
            "relay_id": relay_id,
            "payload": claim.payload,
        });
        if let Some(sim) = claim.payload.get("_sim").and_then(|v| v.as_str()) {
            body["sim"] = json!(sim);
        }
        let resp = self
            .http
            .post(format!("{}/submit", self.base))
            .header("X-Signature", sig)
            .json(&body)
            .send()
            .await;
        self.classify(resp).await
    }

    async fn receipt(
        &self,
        claim: &ClaimResp,
        _relay_id: &str,
    ) -> anyhow::Result<ReceiptResult> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs() as i64;
        let canonical = crypto::receipt_canonical(&claim.id, ts);
        let sig = crypto::hmac_hex(&self.secret, &canonical);
        let url = format!(
            "{}/receipts?id={}&ts={}",
            self.base,
            urlencode(&claim.id),
            ts
        );
        let resp = self
            .http
            .get(url)
            .header("X-Signature", sig)
            .send()
            .await?;
        if resp.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(ReceiptResult::Unknown);
        }
        let resp = resp.error_for_status()?;
        let v: Value = resp.json().await?;
        match v.get("status").and_then(|s| s.as_str()) {
            Some("confirmed") => Ok(ReceiptResult::Confirmed(
                v.get("tx_hash")
                    .and_then(|t| t.as_str())
                    .unwrap_or_default()
                    .to_string(),
            )),
            Some("accepted") => Ok(ReceiptResult::Accepted),
            other => anyhow::bail!("unexpected receipt status: {other:?}"),
        }
    }

    async fn classify(
        &self,
        resp: reqwest::Result<reqwest::Response>,
    ) -> Result<SubmitResult, TargetError> {
        match resp {
            Ok(r) => {
                let status = r.status();
                if status.is_success() {
                    let v: Value = r.json().await.unwrap_or(Value::Null);
                    return match v.get("status").and_then(|s| s.as_str()) {
                        Some("confirmed") => Ok(SubmitResult::Confirmed(
                            v.get("tx_hash")
                                .and_then(|t| t.as_str())
                                .unwrap_or_default()
                                .to_string(),
                        )),
                        Some("accepted") => Ok(SubmitResult::Accepted),
                        _ => Err(TargetError::Transient("malformed success body".into())),
                    };
                }
                let text = r.text().await.unwrap_or_default();
                if status == reqwest::StatusCode::UNPROCESSABLE_ENTITY
                    || status == reqwest::StatusCode::UNAUTHORIZED
                {
                    Err(TargetError::Fatal(status.to_string(), text))
                } else {
                    // 404 / 5xx 等：不确定状态，必须先查询再决定
                    Err(TargetError::Transient(format!("{status}: {text}")))
                }
            }
            Err(e) => {
                // 超时 / 连接错误 / 请求未发出：典型的“未知”，触发查询-重发流程
                Err(TargetError::Transient(e.to_string()))
            }
        }
    }
}

fn urlencode(s: &str) -> String {
    // 提交 ID 允许的字符集外做百分号编码
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
