//! 中继工作循环：领取租约 → 提交目标链 → 回执。
//!
//! 关键规则（与需求逐条对应）：
//! 1. 每次提交都带同一个稳定 `submission_id` 与本代 `fencing_token`；
//! 2. 提交超时/连接失败只代表"结果未知"：先 `GET /result/{id}` 查询，
//!    查询也不通时才带着**原 ID** 自动重发 —— 绝不生成新 ID、绝不把未知当失败；
//! 3. 旧代中继的迟到回执会被协调器以 stale_token 拒绝，状态绝不回退；
//! 4. 租约到期仍未查明结果就放弃本轮，交给协调器重领（新 token 让旧回执失效）；
//! 5. 只有目标明确拒绝（确定性失败）才上报失败回执，阻塞本通道后续。

use crate::client::Client;
use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};

#[derive(Debug, Clone)]
pub struct WorkerConfig {
    pub coordinator_url: String,
    pub target_url: String,
    pub relay_id: String,
    pub lease_ttl_secs: i64,
    pub submit_timeout: Duration,
    pub poll_interval: Duration,
    pub backoff_base: Duration,
}

pub struct Confirmed {
    pub tx_id: String,
    pub result: Value,
    pub replayed: bool,
}

/// 单条租约的处理结果。
#[derive(PartialEq, Eq)]
enum Handled {
    /// 确认/失败回执被接受，或者本代已被新一代取代（不影响正确性）。
    Done,
    /// 租约耗尽，消息留给下一次领取。
    LeaseExpired,
}

pub async fn run_loop(cfg: WorkerConfig, shutdown: Arc<tokio::sync::Notify>) {
    let coord = Client::new(&cfg.coordinator_url, Duration::from_secs(10));

    loop {
        tokio::select! {
            _ = shutdown.notified() => {
                info!(relay = %cfg.relay_id, "shutting down");
                return;
            }
            _ = tokio::time::sleep(cfg.poll_interval) => {}
        }

        let path = format!(
            "/v1/leases/acquire?relay_id={}&ttl_secs={}",
            cfg.relay_id, cfg.lease_ttl_secs
        );
        let lease = match coord.post_opt(&path, json!({})).await {
            Ok(Ok(Some(v))) => v,
            Ok(Ok(None)) => continue, // 204：暂无工作
            Ok(Err(e)) => {
                warn!(relay = %cfg.relay_id, error = %e, "acquire rejected");
                continue;
            }
            Err(e) => {
                warn!(relay = %cfg.relay_id, error = %e, "coordinator unreachable");
                continue;
            }
        };

        info!(
            relay = %cfg.relay_id,
            submission = %lease["submission_id"],
            channel = %lease["channel_id"],
            nonce = lease["nonce"].as_i64().unwrap_or(-1),
            token = lease["fencing_token"].as_i64().unwrap_or(-1),
            attempt = lease["attempts"].as_i64().unwrap_or(-1),
            "lease acquired"
        );

        if handle_lease(&cfg, &coord, lease).await == Handled::LeaseExpired {
            warn!(relay = %cfg.relay_id,
                  "lease expired before outcome was known; another generation takes over");
        }
    }
}

async fn handle_lease(
    cfg: &WorkerConfig,
    coord: &Client,
    lease: Value,
) -> Handled {
    let id = lease["submission_id"].as_str().unwrap().to_string();
    let token = lease["fencing_token"].as_i64().unwrap();
    let expires: DateTime<Utc> =
        serde_json::from_value(lease["lease_expires_at"].clone()).unwrap();

    let submit_body = json!({
        "submission_id": id,
        "channel_id": lease["channel_id"],
        "nonce": lease["nonce"],
        "fencing_token": token,
        "relay_id": cfg.relay_id,
        "payload": lease["payload"],
    });

    // 未知结果时的退避重试严格限制在租约窗口内；
    // 超出后本代退出，新一代会带新 token 用同一 id 继续。
    let mut attempt: u32 = 0;
    loop {
        if Utc::now() >= expires {
            return Handled::LeaseExpired;
        }

        // 关键：本次提交的 HTTP 超时不得超过租约剩余时间，
        // 否则一个租约已失效的旧中继可能在过期后才收到成功响应并抢写状态。
        let remaining_ms = (expires - Utc::now()).num_milliseconds().max(0) as u64;
        if remaining_ms < 200 {
            // 剩余时间不足以完成一次有意义的提交+查询+回执，直接交班。
            return Handled::LeaseExpired;
        }
        let budget = cfg
            .submit_timeout
            .min(Duration::from_millis(remaining_ms.saturating_sub(100)));
        let target = Client::new(&cfg.target_url, budget.max(Duration::from_millis(50)));

        match submit_once(&target, &id, &submit_body).await {
            Ok(Ok(confirmed)) => {
                info!(
                    relay = %cfg.relay_id, submission = %id,
                    tx_id = %confirmed.tx_id, replayed = confirmed.replayed,
                    "target confirmed"
                );
                return send_receipt(
                    coord,
                    &id,
                    json!({
                        "fencing_token": token,
                        "tx_id": confirmed.tx_id,
                        "result": confirmed.result,
                    }),
                    cfg,
                )
                .await;
            }
            Ok(Err(rejection)) => {
                // 确定性拒绝 —— 只有这种情况允许上报失败，阻塞本通道后续。
                warn!(relay = %cfg.relay_id, submission = %id, error = %rejection,
                      "target deterministically rejected");
                return send_receipt(
                    coord,
                    &id,
                    json!({"fencing_token": token, "error": rejection}),
                    cfg,
                )
                .await;
            }
            Err(()) => {
                // 仍是"未知"：在剩余租约内用同一个 submission_id 退避重试。
                attempt += 1;
                let remaining = (expires - Utc::now()).num_milliseconds().max(0) as u64;
                if remaining == 0 {
                    return Handled::LeaseExpired;
                }
                let delay =
                    crate::backoff_delay(cfg.backoff_base.as_millis() as u64, attempt, 5_000)
                        .as_millis() as u64;
                tokio::time::sleep(Duration::from_millis(delay.min(remaining))).await;
            }
        }
    }
}

/// 一次"提交或查明"尝试：
/// - 目标确认（含幂等重放/查询命中）→ Ok(Ok(confirmed))
/// - 目标确定性拒绝 → Ok(Err(error))
/// - 结果未知（超时/连不通/意外状态/404/查询不通）→ Err(())，
///   调用方必须带**同一个 submission_id** 重发。
async fn submit_once(
    target: &Client,
    id: &str,
    body: &Value,
) -> Result<Result<Confirmed, String>, ()> {
    match target.post("/submit", body.clone()).await {
        Ok((200, v)) if v.get("ok").and_then(|x| x.as_bool()) == Some(true) => {
            let tx_id = v["tx_id"].as_str().unwrap_or_default().to_string();
            let replayed = v["replayed"].as_bool().unwrap_or(false);
            Ok(Ok(Confirmed {
                tx_id,
                result: v,
                replayed,
            }))
        }
        Ok((422, v)) => Ok(Err(v["error"]
            .as_str()
            .unwrap_or("target rejected")
            .to_string())),
        Ok((status, v)) => {
            warn!(status, body = %v, "unexpected target status; query before resend");
            resolve_by_query(target, id).await
        }
        Err(e) => {
            warn!(error = %e, "submit timed out / unreachable; query before resend");
            resolve_by_query(target, id).await
        }
    }
}

async fn resolve_by_query(
    target: &Client,
    id: &str,
) -> Result<Result<Confirmed, String>, ()> {
    match target.get_opt(&format!("/result/{id}")).await {
        Ok(Some(r)) if r["status"] == "confirmed" => Ok(Ok(Confirmed {
            tx_id: r["tx_id"].as_str().unwrap_or_default().to_string(),
            result: r,
            replayed: true,
        })),
        Ok(Some(r)) => {
            warn!(body = %r, "query says the submission failed");
            Ok(Err(r["error"]
                .as_str()
                .unwrap_or("target reports failure")
                .to_string()))
        }
        // 404（None）与查询不通（Err）都只是"未知"：原 ID 重发，不生成新 ID。
        Ok(None) => Err(()),
        Err(e) => {
            warn!(error = %e, "result query also failed; resend with same id");
            Err(())
        }
    }
}

async fn send_receipt(
    coord: &Client,
    id: &str,
    body: Value,
    cfg: &WorkerConfig,
) -> Handled {
    // 回执发往协调器也可能遇到网络抖动：短重试；语义由服务端 fencing 裁决。
    for attempt in 0..5 {
        match coord.post(&format!("/v1/leases/{id}/receipt"), body.clone()).await {
            Ok((200, v)) => {
                let accepted = v["accepted"].as_bool().unwrap_or(false);
                let verdict = v["verdict"].as_str().unwrap_or("?");
                if accepted {
                    return Handled::Done;
                }
                // stale_token / already_final：新一代已接管。本代到此结束，
                // 绝不能重试或绕过裁决 —— 旧状态不得覆盖新代状态。
                warn!(
                    relay = %cfg.relay_id, submission = %id, verdict,
                    "receipt superseded; coordinator keeps newer generation"
                );
                return Handled::Done;
            }
            Ok((status, v)) => warn!(status, body = %v, "receipt HTTP error"),
            Err(e) => warn!(error = %e, "receipt delivery failed, retrying"),
        }
        tokio::time::sleep(Duration::from_millis(200u64.saturating_mul(attempt as u64 + 1))).await;
    }
    warn!(submission = %id, "receipt undeliverable; lease fencing keeps state safe");
    Handled::LeaseExpired
}
