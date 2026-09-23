//! 端到端场景：真实进程 + 真实 Postgres + 真实 HTTP/HMAC。
//!
//! 覆盖：
//!  1. 基本成功 + nonce 连续
//!  2. 提交成功但响应丢失（drop_first）-> 超时先查 receipts -> 收敛确认，目标仅一笔
//!  3. 瞬时失败后重试（fail_attempts），同一提交 ID、同一 nonce，目标仅一笔
//!  4. 租约过期 + 跨进程接管：旧中继被强杀（stall），新中继重领，fence 递增
//!  5. fencing：旧代次回执（旧 fence）被协调器 409 拒绝，不能覆盖新代次状态
//!  6. 致命失败阻塞本通道后续，但不阻塞其他通道；requeue 后恢复推进
//!  7. 跨进程并发竞争：多中继同时领，每条只被一个中继领走，fence/nonce 不乱
//!  8. 协调器重启后的恢复（状态持久化在 Postgres）
//!  9. 目标桩按 ID 查询：未知 ID 返回 404，不能当失败造新 ID
mod common;

use common::*;
use serde_json::{json, Value};
use std::time::Duration;

fn commit(channel: &str, name: &str, extra: Option<Value>) -> Value {
    let id = format!("{channel}-{name}");
    let mut payload = json!({"v": name});
    if let Some(e) = extra {
        payload["_sim"] = e;
    }
    json!({"id": id, "payload": payload})
}

#[tokio::test]
async fn t1_happy_path_contiguous_nonces() {
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-ok");
    let commits = json!([
        commit(&ch, "m0", None),
        commit(&ch, "m1", None),
        commit(&ch, "m2", None),
    ]);
    enqueue(&http, &svc, &ch, commits).await;

    let relay = svc.relay("relay-happy", &[]);
    for (i, name) in ["m0", "m1", "m2"].iter().enumerate() {
        let id = format!("{ch}-{name}");
        let sub = wait_status(&http, &svc, &id, "confirmed", 60).await;
        assert_eq!(sub["nonce"].as_i64().unwrap(), i as i64, "nonce {i}");
        assert_eq!(sub["fence"].as_i64().unwrap(), 1, "no re-claims expected");
        assert_eq!(sub["tx_hash"].as_str().unwrap().len(), 64);
    }
    kill(relay);

    let entries = stub_entries(&http, &svc).await;
    let mine: Vec<_> = entries.iter().filter(|e| e["id"].as_str().unwrap().starts_with(&ch)).collect();
    assert_eq!(mine.len(), 3, "target must record each commit exactly once");
    let mut nonces: Vec<i64> = mine.iter().map(|e| e["nonce"].as_i64().unwrap()).collect();
    nonces.sort_unstable();
    assert_eq!(nonces, vec![0, 1, 2]);
}

#[tokio::test]
async fn t2_response_lost_after_durable_submit() {
    // 桩延迟 5000ms 返回（> worker 2s 超时），但已入账。
    // worker 超时 -> 先 receipts 查询（此时已能查到 confirmed）-> 用同一笔完成。
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-drop");
    let commits = json!([commit(&ch, "m0", Some(json!("drop_first:5000")))]);
    enqueue(&http, &svc, &ch, commits).await;

    let relay = svc.relay("relay-drop", &[]);
    let id = format!("{ch}-m0");
    let sub = wait_status(&http, &svc, &id, "confirmed", 80).await;
    assert_eq!(sub["nonce"].as_i64().unwrap(), 0);
    kill(relay);

    // 目标端只有一笔（查询恢复，绝无重复入账、绝无新 ID）
    let entries = stub_entries(&http, &svc).await;
    let mine: Vec<_> = entries.iter().filter(|e| e["id"].as_str().unwrap() == id).collect();
    assert_eq!(mine.len(), 1, "exactly one target entry after lost-response recovery");
    assert_eq!(mine[0]["status"].as_str(), Some("confirmed"));
}

#[tokio::test]
async fn t3_transient_failures_same_id_same_nonce() {
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-retry");
    let commits = json!([commit(&ch, "m0", Some(json!("fail_attempts:3")))]);
    enqueue(&http, &svc, &ch, commits).await;

    let relay = svc.relay("relay-retry", &[]);
    let id = format!("{ch}-m0");
    let sub = wait_status(&http, &svc, &id, "confirmed", 80).await;
    assert_eq!(sub["nonce"].as_i64().unwrap(), 0, "nonce stable across retries");
    kill(relay);

    let entries = stub_entries(&http, &svc).await;
    let mine: Vec<_> = entries.iter().filter(|e| e["id"].as_str().unwrap() == id).collect();
    assert_eq!(mine.len(), 1, "retries must not create duplicate target commits");
}

#[tokio::test]
async fn t4_lease_expiry_cross_process_takeover() {
    // 2s 短租约：relay-A 领取后 stall（不心跳），强杀；租约过期后 relay-B 重领。
    let svc = Services::start(2, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-lease");
    enqueue(&http, &svc, &ch, json!([commit(&ch, "m0", None)])).await;

    let a = svc.relay("relay-A", &["--stall-after-claim"]);
    let id = format!("{ch}-m0");
    // A 已领走：状态 leased、fence 1
    let leased = wait_status(&http, &svc, &id, "leased", 30).await;
    assert_eq!(leased["fence"].as_i64().unwrap(), 1);
    assert_eq!(leased["leased_by"].as_str(), Some("relay-A"));

    // 强杀 A（模拟崩溃）
    kill(a);

    // B 在租约过期后接管
    let b = svc.relay("relay-B", &[]);
    let sub = wait_status(&http, &svc, &id, "confirmed", 60).await;
    assert!(sub["fence"].as_i64().unwrap() >= 2, "re-claim must bump fence");
    assert_eq!(sub["leased_by"].as_str(), Some("relay-B"));
    assert_eq!(sub["nonce"].as_i64().unwrap(), 0, "nonce reused on re-claim");
    kill(b);
}

#[tokio::test]
async fn t5_stale_fence_receipt_is_rejected() {
    // 直接驱动协调器 API：A 以 fence 1 持有，过期后 B 以 fence 2 接管，
    // A 的旧 fence=1 complete 必须 409，且不得覆盖 B 建立的状态。
    let svc = Services::start(2, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-fence");
    enqueue(&http, &svc, &ch, json!([commit(&ch, "m0", None)])).await;
    let id = format!("{ch}-m0");

    // 用 internal API 手动领取（不启 worker，便于精确控制两代）
    async fn claim(http: &reqwest::Client, svc: &Services, relay: &str) -> Value {
        http.post(format!("{}/internal/claim", svc.coord_url()))
            .json(&json!({"relay_id": relay}))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap()
    }

    let c1 = claim(&http, &svc, "relay-A").await;
    assert_eq!(c1["fence"].as_i64().unwrap(), 1);
    assert_eq!(c1["nonce"].as_i64().unwrap(), 0);

    // 等租约过期，B 接管
    tokio::time::sleep(Duration::from_secs(3)).await;
    let c2 = claim(&http, &svc, "relay-B").await;
    assert_eq!(c2["fence"].as_i64().unwrap(), 2, "new generation fence");
    assert_eq!(c2["id"].as_str().unwrap(), id);

    // A 的旧代次心跳 / delivered / complete 全部 409
    for path in ["heartbeat", "delivered"] {
        let r = http
            .post(format!("{}/internal/{path}", svc.coord_url()))
            .json(&json!({"id": id, "fence": 1, "relay_id": "relay-A"}))
            .send()
            .await
            .unwrap();
        assert_eq!(r.status().as_u16(), 409, "{path} with stale fence must 409");
    }
    let r = http
        .post(format!("{}/internal/complete", svc.coord_url()))
        .json(&json!({"id": id, "fence": 1, "relay_id": "relay-A", "tx_hash": "stale-hash"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 409);

    // 状态没有被旧代次污染：仍 leased by B，fence 2，无 tx_hash
    let cur = get_sub(&http, &svc, &id).await;
    assert_eq!(cur["fence"].as_i64().unwrap(), 2);
    assert_eq!(cur["leased_by"].as_str(), Some("relay-B"));
    assert!(cur["tx_hash"].is_null());

    // B 用 fence 2 正常完成
    let r = http
        .post(format!("{}/internal/complete", svc.coord_url()))
        .json(&json!({"id": id, "fence": 2, "relay_id": "relay-B", "tx_hash": "b-hash"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 200);
    let done = get_sub(&http, &svc, &id).await;
    assert_eq!(done["status"].as_str(), Some("confirmed"));
    assert_eq!(done["tx_hash"].as_str(), Some("b-hash"));
}

#[tokio::test]
async fn t6_fatal_blocks_channel_but_not_others() {
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    // 通道 X：第一条“一次性致命”（桩只在首次提交时 422，重发成功），后跟一条正常。
    let chx = unique("chX");
    enqueue(
        &http,
        &svc,
        &chx,
        json!([
            commit(&chx, "x0", Some(json!("fatal_once"))),
            commit(&chx, "x1", None),
        ]),
    )
    .await;
    // 通道 Y：正常消息，不受 X 阻塞影响
    let chy = unique("chY");
    enqueue(&http, &svc, &chy, json!([commit(&chy, "y0", None)])).await;

    let relay = svc.relay("relay-block", &[]);

    let x0 = wait_status(&http, &svc, &format!("{chx}-x0"), "failed", 60).await;
    assert!(x0["last_error"].as_str().unwrap().contains("rejected"));

    // Y 的消息照常确认（不被其他通道阻塞）
    let _y0 = wait_status(&http, &svc, &format!("{chy}-y0"), "confirmed", 60).await;

    // 给足时间证明 X 的 x1 没有越过失败队头
    tokio::time::sleep(Duration::from_secs(3)).await;
    let x1 = get_sub(&http, &svc, &format!("{chx}-x1")).await;
    assert_eq!(x1["status"].as_str(), Some("pending"), "x1 blocked behind failed head");
    assert!(x1["nonce"].is_null(), "blocked message never got a nonce");
    kill(relay);

    // 人工 requeue 解除阻塞（fence 再 +1），新中继重发 x0：桩这次放行，
    // x0 与被阻塞的 x1 随后都确认——通道完全恢复。
    let r = http
        .post(format!("{}/submissions/{chx}-x0/requeue", svc.coord_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 200);

    let relay2 = svc.relay("relay-recover", &[]);
    let x0ok = wait_status(&http, &svc, &format!("{chx}-x0"), "confirmed", 60).await;
    assert_eq!(x0ok["nonce"].as_i64().unwrap(), 0, "nonce preserved across fatal/requeue");
    assert!(x0ok["fence"].as_i64().unwrap() >= 2, "requeue bumps fence");
    let x1ok = wait_status(&http, &svc, &format!("{chx}-x1"), "confirmed", 60).await;
    assert_eq!(x1ok["nonce"].as_i64().unwrap(), 1, "channel resumes in order");
    kill(relay2);
}

#[tokio::test]
async fn t7_concurrent_relays_disjoint_claims() {
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-conc");
    let n = 6;
    let mut commits = vec![];
    for i in 0..n {
        commits.push(commit(&ch, &format!("m{i}"), None));
    }
    enqueue(&http, &svc, &ch, json!(commits)).await;

    // 三个中继并发抢同一通道（必须串行成功，但绝不能重复领取/乱序）
    let mut relays: Vec<_> = (0..3).map(|i| svc.relay(&format!("relay-c{i}"), &[])).collect();

    for i in 0..n {
        let id = format!("{ch}-m{i}");
        let sub = wait_status(&http, &svc, &id, "confirmed", 90).await;
        assert_eq!(sub["nonce"].as_i64().unwrap(), i as i64, "strict nonce order");
        assert_eq!(sub["fence"].as_i64().unwrap(), 1, "no double claim under healthy leases");
    }
    for r in relays.drain(..) {
        kill(r);
    }

    let entries = stub_entries(&http, &svc).await;
    let mine: Vec<_> = entries.iter().filter(|e| e["id"].as_str().unwrap().starts_with(&ch)).collect();
    assert_eq!(mine.len(), n as usize);
}

#[tokio::test]
async fn t8_coordinator_restart_recovers_state() {
    let mut svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-restart");
    enqueue(&http, &svc, &ch, json!([commit(&ch, "m0", None), commit(&ch, "m1", None)])).await;

    let relay = svc.relay("relay-rs", &[]);
    let _ = wait_status(&http, &svc, &format!("{ch}-m0"), "confirmed", 60).await;
    // 杀掉协调器（relay 会短暂报错重试）
    svc.coord_kill();
    tokio::time::sleep(Duration::from_secs(1)).await;
    svc.restart_coordinator().await;

    // m1 最终仍确认，nonce 连续，状态从 Postgres 恢复
    let m1 = wait_status(&http, &svc, &format!("{ch}-m1"), "confirmed", 90).await;
    assert_eq!(m1["nonce"].as_i64().unwrap(), 1);
    kill(relay);
}

#[tokio::test]
async fn t9_unknown_commit_is_404_not_failure() {
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;

    // 直接对桩用合法签名查询一个从不曾提交的 ID：必须 404
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let id = format!("{}-never-existed", unique("ghost"));
    let canonical = format!("GET\n/receipts\n{id}\n{ts}");
    let sig = common_sign(&canonical);
    let r = http
        .get(format!(
            "{}/receipts?id={}&ts={}",
            svc.stub_url(),
            id,
            ts
        ))
        .header("X-Signature", sig)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 404);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["error"].as_str(), Some("unknown_commit"));
}

fn common_sign(canonical: &str) -> String {
    // 与 crate::crypto 相同的 HMAC-SHA256（测试侧独立计算，验证真实密码学）
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    type H = Hmac<Sha256>;
    let mut mac = H::new_from_slice(SECRET.as_bytes()).unwrap();
    mac.update(canonical.as_bytes());
    hex_encode(&mac.finalize().into_bytes())
}

#[tokio::test]
async fn t10_claim_race_single_winner() {
    // 20 个并发 claim 抢同一条：恰好一个 200 且 fence=1，其余 204。
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-race");
    enqueue(&http, &svc, &ch, json!([commit(&ch, "m0", None)])).await;

    let mut futs = vec![];
    for i in 0..20 {
        let c = svc.coord_url();
        let http = http.clone();
        futs.push(tokio::spawn(async move {
            http.post(format!("{c}/internal/claim"))
                .json(&json!({"relay_id": format!("racer-{i}")}))
                .send()
                .await
                .unwrap()
                .status()
                .as_u16()
        }));
    }
    let statuses: Vec<u16> = futures::future::join_all(futs)
        .await
        .into_iter()
        .map(|r| r.unwrap())
        .collect();
    let winners = statuses.iter().filter(|&&s| s == 200).count();
    let empty = statuses.iter().filter(|&&s| s == 204).count();
    assert_eq!(winners, 1, "exactly one relay must win the lease");
    assert_eq!(empty, 19);

    // 赢者状态一致：fence=1、有 nonce、leased_by 是某个 racer
    let sub = get_sub(&http, &svc, &format!("{ch}-m0")).await;
    assert_eq!(sub["fence"].as_i64().unwrap(), 1);
    assert_eq!(sub["nonce"].as_i64().unwrap(), 0);
    assert!(sub["leased_by"].as_str().unwrap().starts_with("racer-"));
    assert_eq!(sub["attempts"].as_i64().unwrap(), 1);
}

#[tokio::test]
async fn t11_target_rejects_nonce_gap_and_bad_signature() {
    // 目标桩真实执行 nonce 连续性与 HMAC 校验：
    // - 首笔必须 nonce=0；直接拿 nonce=5 提交 -> 422 nonce_gap；
    // - 错误签名 -> 401。
    let svc = Services::start(10, "normal").await;
    let http = svc.http().await;
    let ch = unique("ch-gap");

    // 正确签名但 nonce 跳跃
    let body = json!({
        "id": format!("{ch}-bad"), "channel_id": ch, "nonce": 5,
        "fence": 1, "relay_id": "r", "payload": {"v": 1},
    });
    let canonical = format!("POST\n/submit\nr\n1\n5\n{ch}\n{}-bad\n{}", ch,
        serde_json::to_string(&json!({"v":1})).unwrap());
    let sig = common_sign(&canonical);
    let r = http
        .post(format!("{}/submit", svc.stub_url()))
        .header("X-Signature", sig)
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 422);
    assert_eq!(r.json::<Value>().await.unwrap()["error"], "nonce_gap");

    // 签名错误
    let r = http
        .post(format!("{}/submit", svc.stub_url()))
        .header("X-Signature", "deadbeef")
        .json(&json!({
            "id": format!("{ch}-x"), "channel_id": ch, "nonce": 0,
            "fence": 1, "relay_id": "r", "payload": {"v": 1},
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status().as_u16(), 401);
}

fn hex_encode(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{x:02x}"));
    }
    s
}
