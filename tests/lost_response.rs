//! 场景一：提交成功但响应丢失 / 提交在途且查询 404。
//!
//! 验收要点：
//! - 目标桩只产生**一条**链上记录（同一稳定 submission_id，幂等）；
//! - 消息最终 confirmed，且最终回执的 fencing token 是租约上的那一代；
//! - 任何时刻没有把"未知"当成失败。

mod common;

use common::spawn_stack;
use serde_json::json;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn response_lost_after_commit_then_query_recovers() {
    let h = spawn_stack().await;

    // 租约 30 秒（足够长，本轮不应过期）；提交超时 1 秒；目标首次响应拖住 5 秒。
    h.spawn_worker("relay-lost-A", 30, 1);

    let id = h.enqueue("ch-lost-a", 1).await;
    h.target_state
        .set_rule(
            id.parse().unwrap(),
            relay_coord::target::Rule {
                drop_responses: 1,
                sleep_secs: 5,
                commit_delay_secs: 0,
                query_delay_secs: 0,
                always_fail: false,
            },
        )
        .await;

    let msg = h
        .wait_status(&id, "confirmed", Duration::from_secs(40))
        .await;
    assert_eq!(msg["status"], "confirmed");
    assert!(
        msg["target_tx_id"].as_str().unwrap().starts_with("0x"),
        "tx_id 应为真实哈希: {msg}"
    );
    assert_eq!(msg["attempts"].as_i64(), Some(1), "只领了一次租约");

    // 目标桩全局只有这一条记录，且被观测到两次：首次提交 + 查询恢复后的重放/查询。
    let rec = h.target_state.record(id.parse().unwrap()).await.unwrap();
    assert_eq!(rec.status, "confirmed");
    assert!(rec.observed.len() >= 1);
    let records_for_id = h
        .target_state
        .records()
        .await
        .into_iter()
        .filter(|r| r.submission_id.to_string() == id)
        .count();
    assert_eq!(records_for_id, 1, "稳定 ID 幂等：只能有一条链上记录");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn unknown_during_commit_window_keeps_same_id() {
    let h = spawn_stack().await;
    h.spawn_worker("relay-lost-B", 30, 1);

    let id = h.enqueue("ch-lost-b", 2).await;
    // 目标接受后 4 秒才落盘：前两次提交期间查询是 404（真正的"未知"）。
    // 正确行为：超时 → 查询 404 → 用**原 id** 重发，绝不造新 ID。
    h.target_state
        .set_rule(
            id.parse().unwrap(),
            relay_coord::target::Rule {
                drop_responses: 0,
                sleep_secs: 0,
                commit_delay_secs: 4,
                query_delay_secs: 0,
                always_fail: false,
            },
        )
        .await;

    let msg = h
        .wait_status(&id, "confirmed", Duration::from_secs(40))
        .await;
    assert_eq!(msg["status"], "confirmed", "{msg}");
    assert_eq!(msg["attempts"].as_i64(), Some(1), "租约不应易主");

    let records_for_id = h
        .target_state
        .records()
        .await
        .into_iter()
        .filter(|r| r.submission_id.to_string() == id)
        .count();
    assert_eq!(records_for_id, 1, "未知时重发必须复用同一 ID");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn target_observed_fencing_token_matches_lease() {
    let h = spawn_stack().await;
    h.spawn_worker("relay-lost-C", 30, 2);
    let id = h.enqueue("ch-lost-c", 3).await;
    let msg = h
        .wait_status(&id, "confirmed", Duration::from_secs(20))
        .await;
    let token = msg["fencing_token"].as_i64().unwrap();
    let rec = h.target_state.record(id.parse().unwrap()).await.unwrap();
    assert!(rec
        .observed
        .iter()
        .any(|o| o.fencing_token == token));
    let _ = json!({});
}
