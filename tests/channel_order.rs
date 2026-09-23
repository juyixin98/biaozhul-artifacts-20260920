//! 场景四：每通道 nonce 顺序 + 失败队头阻塞 + 不波及其他通道 + 恢复。

mod common;

use common::spawn_stack;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn head_of_line_failure_blocks_only_its_channel_and_retry_resumes() {
    let h = spawn_stack().await;
    h.spawn_worker("relay-hol", 30, 2);

    // 通道 X：nonce 1 会被链上确定性拒绝；nonce 2、3 必须在它后面排队。
    // 通道 Y：一切正常，完全不受 X 影响。
    let x0 = h.enqueue("ch-X", 0).await;
    let x1 = h.enqueue("ch-X", 1).await;
    let x2 = h.enqueue("ch-X", 2).await;
    let y0 = h.enqueue("ch-Y", 0).await;
    let y1 = h.enqueue("ch-Y", 1).await;

    h.target_state
        .set_rule(
            x1.parse().unwrap(),
            relay_coord::target::Rule {
                drop_responses: 0,
                sleep_secs: 0,
                commit_delay_secs: 0,
                query_delay_secs: 0,
                always_fail: true,
            },
        )
        .await;

    // Y 通道两条全部确认；X 的 nonce 0 确认、nonce 1 失败、nonce 2 排队。
    h.wait_status(&y0, "confirmed", Duration::from_secs(20)).await;
    h.wait_status(&y1, "confirmed", Duration::from_secs(20)).await;
    h.wait_status(&x0, "confirmed", Duration::from_secs(20)).await;
    let x1m = h.wait_status(&x1, "failed", Duration::from_secs(20)).await;
    assert!(x1m["error"].as_str().unwrap().contains("rejected"));

    // 给足时间，确保 x2 不会被错误地越过队头提交。
    tokio::time::sleep(Duration::from_secs(2)).await;
    let x2_stuck = h.status_of(&x2).await;
    assert!(
        x2_stuck["status"] == "pending" || x2_stuck["status"] == "leased",
        "队头失败后，后续 nonce 不得越过：{x2_stuck}"
    );

    let (_, channels) = h.client().get("/v1/channels").await.unwrap();
    let find = |name: &str| {
        channels
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["channel_id"] == name)
            .unwrap()
            .clone()
    };
    assert_eq!(find("ch-X")["blocked"], true);
    assert_eq!(find("ch-Y")["blocked"], false, "其他通道不受影响");

    // 新消息在 blocked 通道上被明确拒绝（不能悄悄收下假装排队）。
    let (status, reject) = h
        .client()
        .post(
            "/v1/messages",
            serde_json::json!({"channel_id": "ch-X", "payload": {"x": 99}}),
        )
        .await
        .unwrap();
    assert_eq!(status, 409, "{reject}");
    assert_eq!(reject["error"], "channel_blocked");

    // 恢复：解除目标桩上的故障规则 → retry x1 → x1、x2 顺序确认，通道解锁。
    h.target_state.clear_rule(x1.parse().unwrap()).await;

    let (status, retried) = h.client().post(&format!("/v1/messages/{x1}/retry"), serde_json::json!({})).await.unwrap();
    assert_eq!(status, 200, "{retried}");

    h.wait_status(&x1, "confirmed", Duration::from_secs(20)).await;
    let x2m = h.wait_status(&x2, "confirmed", Duration::from_secs(20)).await;
    assert_eq!(x2m["nonce"], 2);

    let (_, channels) = h.client().get("/v1/channels").await.unwrap();
    let find = |name: &str| {
        channels
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["channel_id"] == name)
            .unwrap()
            .clone()
    };
    assert_eq!(find("ch-X")["blocked"], false);

    // 对非 failed 消息 retry 必须报错。
    let (status, bad) = h
        .client()
        .post(&format!("/v1/messages/{x0}/retry"), serde_json::json!({}))
        .await
        .unwrap();
    assert_eq!(status, 409, "{bad}");
    assert_eq!(bad["error"], "not_failed");
}
