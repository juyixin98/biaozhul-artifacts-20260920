//! 场景二：租约过期重领 + fencing token 防旧代覆盖。

mod common;

use common::spawn_stack;
use relay_coord::client::Client;
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stale_relay_receipt_cannot_overwrite_new_generation() {
    let h = spawn_stack().await;

    // 先不放 worker：手动领取第一代租约（TTL 1 秒），模拟中继 A 领走后假死。
    let coord = Client::new(&h.coord_url, Duration::from_secs(10));
    let id = h.enqueue("ch-fence-a", 1).await;

    let lease_a = coord
        .post_opt(
            "/v1/leases/acquire?relay_id=relay-stale-A&ttl_secs=1",
            serde_json::json!({}),
        )
        .await
        .unwrap()
        .unwrap()
        .expect("200 with lease body");
    let token_a = lease_a["fencing_token"].as_i64().unwrap();
    assert!(token_a > 0);

    // 第一代租约未过期时再次领取：消息不可被重复领取（204）。
    let again = coord
        .post_opt(
            "/v1/leases/acquire?relay_id=relay-stale-B&ttl_secs=5",
            serde_json::json!({}),
        )
        .await
        .unwrap()
        .unwrap();
    assert!(again.is_none(), "未过期租约不得被他人领取");

    // 等 A 的租约过期，由 B 领取第二代（此时消息仍为 leased，未终态）。
    tokio::time::sleep(Duration::from_millis(1_300)).await;
    let lease_b = coord
        .post_opt(
            "/v1/leases/acquire?relay_id=relay-fresh-B&ttl_secs=10",
            serde_json::json!({}),
        )
        .await
        .unwrap()
        .unwrap()
        .expect("过期后必须能重领");
    let token_b = lease_b["fencing_token"].as_i64().unwrap();
    assert!(token_b > token_a, "新代 fencing token 必须严格递增");

    // A 此时醒来，拿着旧 token 上报确认回执（甚至伪造自己的 tx_id）。
    // 消息仍是 leased，裁决必须是 stale_token，状态不得被旧代改写。
    let (status, receipt) = coord
        .post(
            &format!("/v1/leases/{id}/receipt"),
            serde_json::json!({
                "fencing_token": token_a,
                "tx_id": "0xFAKE_FROM_STALE_RELAY",
                "result": {"note": "stale generation"},
            }),
        )
        .await
        .unwrap();
    assert_eq!(status, 200);
    assert_eq!(receipt["accepted"], false);
    assert_eq!(receipt["verdict"], "stale_token");

    // 消息仍是 leased，没有被 A 的伪造交易污染。
    let mid = h.status_of(&id).await;
    assert_eq!(mid["status"], "leased");
    assert!(mid["target_tx_id"].is_null());
    assert_eq!(mid["fencing_token"].as_i64(), Some(token_b));

    // 旧代再报失败也必须被拒，通道不能被旧代拖垮。
    let (_, fail_receipt) = coord
        .post(
            &format!("/v1/leases/{id}/receipt"),
            serde_json::json!({"fencing_token": token_a, "error": "late failure from A"}),
        )
        .await
        .unwrap();
    assert_eq!(fail_receipt["verdict"], "stale_token");
    assert_eq!(fail_receipt["accepted"], false);

    // 当代 B 正常提交目标并回执确认。
    h.spawn_worker("relay-fresh-B-live", 10, 2);
    // B 已持有租约，worker 要等其过期后再领第三代；为加快，直接用 token_b 模拟 B 的回执。
    // 先让 worker 路径也跑一遍（更贴近真实）：等待 worker 领取新一代并确认。
    let msg = h
        .wait_status(&id, "confirmed", Duration::from_secs(30))
        .await;
    let token_final = msg["fencing_token"].as_i64().unwrap();
    assert!(token_final >= token_b);
    let tx_final = msg["target_tx_id"].as_str().unwrap().to_string();

    // 终态之后旧代的任何回执都是 already_final（幂等无害但不接受）。
    let (_, late) = coord
        .post(
            &format!("/v1/leases/{id}/receipt"),
            serde_json::json!({
                "fencing_token": token_a,
                "tx_id": "0xFAKE_LATE",
                "result": {},
            }),
        )
        .await
        .unwrap();
    assert_eq!(late["verdict"], "already_final");

    // 最终状态纹丝不动。
    let after = h.status_of(&id).await;
    assert_eq!(after["status"], "confirmed");
    assert_eq!(after["target_tx_id"].as_str().unwrap(), tx_final);

    // 目标桩只有一条链上记录（稳定 submission_id 幂等）。
    assert_eq!(
        h.target_state
            .records()
            .await
            .into_iter()
            .filter(|r| r.submission_id.to_string() == id)
            .count(),
        1
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn slow_relay_with_expiring_lease_hands_over_cleanly() {
    // 目标对该消息首次响应拖住 5 秒，租约只有 2 秒，worker A 在窗口内无法确认；
    // 租约过期后由新一代接手，通过查询/重放确认。
    let h = spawn_stack().await;

    let id = h.enqueue("ch-fence-b", 1).await;
    // 先配故障再放 worker：首次提交已落盘但响应拖住 5 秒，结果查询也拖 5 秒。
    // 2 秒租约的第一代既收不到响应也查不到结果，必须安全交班；
    // 第二代重放/查询命中后确认，全程只有一个稳定 submission_id。
    h.target_state
        .set_rule(
            id.parse().unwrap(),
            relay_coord::target::Rule {
                drop_responses: 1,
                sleep_secs: 5,
                commit_delay_secs: 0,
                query_delay_secs: 5,
                always_fail: false,
            },
        )
        .await;

    h.spawn_worker("relay-slow-A", 2, 1);
    let msg = h
        .wait_status(&id, "confirmed", Duration::from_secs(40))
        .await;
    assert!(
        msg["attempts"].as_i64().unwrap() >= 2,
        "应至少发生两代领取，实际 attempts={}",
        msg["attempts"]
    );

    let rec = h.target_state.record(id.parse().unwrap()).await.unwrap();
    let tokens: Vec<i64> = rec.observed.iter().map(|o| o.fencing_token).collect();
    assert!(
        tokens.windows(2).all(|w| w[0] <= w[1]),
        "目标看到的 token 序列非递减: {tokens:?}"
    );
    assert_eq!(
        h.target_state
            .records()
            .await
            .iter()
            .filter(|r| r.submission_id.to_string() == id)
            .count(),
        1
    );
}
