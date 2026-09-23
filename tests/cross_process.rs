//! 场景三：跨进程竞争 —— 三个真正的独立 OS worker 进程同时跑，
//! 抢同一批消息。验证：
//! - 每条消息恰好确认一次（目标桩恰好一条记录、一个 tx）；
//! - 同通道 nonce 严格按序上链（用 observed 的提交顺序/确认时间检验）；
//! - 每个通道的 nonce 连续无空洞；
//! - 负载真实：payload_hash 与提交内容一致。

mod common;

use common::spawn_stack;
use sha2::{Digest, Sha256};
use std::time::Duration;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn three_worker_processes_compete_safely() {
    let h = spawn_stack().await;

    // 真正的 cargo 编译产物进程（不是 tokio task）。
    let mut children = vec![];
    for i in 0..3 {
        children.push(h.spawn_worker_process(
            &format!("relay-proc-{i}"),
            10,
            2,
        ));
    }

    const CHANNELS: [&str; 3] = ["cp-a", "cp-b", "cp-c"];
    const PER_CHANNEL: i64 = 8;
    let mut ids = vec![];
    for (ci, ch) in CHANNELS.iter().enumerate() {
        for n in 0..PER_CHANNEL {
            ids.push((ch.to_string(), n, h.enqueue(ch, (ci as i64) * 100 + n).await));
        }
    }

    for (_, _, id) in &ids {
        h.wait_status(id, "confirmed", Duration::from_secs(60)).await;
    }
    for c in children.iter_mut() {
        let _ = c.kill();
    }

    let records = h.target_state.records().await;
    let by_channel: std::collections::HashMap<&str, Vec<_>> =
        CHANNELS.iter().map(|c| (*c, vec![])).collect();
    let mut by_channel = by_channel;
    for r in &records {
        if let Some(v) = by_channel.get_mut(r.channel_id.as_str()) {
            v.push(r);
        }
    }

    for ch in CHANNELS {
        let mut v = by_channel.remove(ch).unwrap();
        v.sort_by_key(|r| r.nonce);
        assert_eq!(v.len(), PER_CHANNEL as usize, "{ch} 每条 nonce 恰好一条链上记录");

        // nonce 连续 0..N，且 tx_id 互不相同（恰好提交一次）。
        let mut txs = std::collections::HashSet::new();
        for (i, r) in v.iter().enumerate() {
            assert_eq!(r.nonce, i as i64, "{ch} nonce 必须连续无空洞");
            assert_eq!(r.status, "confirmed");
            assert!(txs.insert(r.tx_id.clone().unwrap()), "tx_id 重复：消息被提交两次");

            // 密码学负载真实性：tx_id = 0x + sha256(id|channel|nonce|payload)。
            let msg = h.status_of(&r.submission_id.to_string()).await;
            let mut hasher = Sha256::new();
            hasher.update(r.submission_id.as_bytes());
            hasher.update(b"|");
            hasher.update(ch.as_bytes());
            hasher.update(b"|");
            hasher.update((r.nonce as i64).to_be_bytes());
            hasher.update(b"|");
            hasher.update(serde_json::to_vec(&msg["payload"]).unwrap());
            let expect = format!("0x{}", hex::encode(hasher.finalize()));
            assert_eq!(r.tx_id.as_deref(), Some(expect.as_str()), "tx 哈希必须真实对应负载");
        }

        // 严格顺序：上链确认时间随 nonce 非递减（同通道）。
        for w in v.windows(2) {
            assert!(w[1].confirmed_at.unwrap() >= w[0].confirmed_at.unwrap(),
                "{ch} nonce {} 在 {} 之前确认，违反每通道顺序", w[0].nonce, w[1].nonce);
        }

        // 每条记录都曾被恰好一个 relay 成功处理；token 观测序列非递减。
        for r in &v {
            let tokens: Vec<i64> = r.observed.iter().map(|o| o.fencing_token).collect();
            assert!(tokens.windows(2).all(|w| w[0] <= w[1]));
        }
    }

    // 协调器侧状态全部 confirmed，通道未阻塞。
    let (_, channels) = h.client().get("/v1/channels").await.unwrap();
    for c in channels.as_array().unwrap() {
        if CHANNELS.contains(&c["channel_id"].as_str().unwrap()) {
            assert_eq!(c["blocked"], false);
            assert_eq!(c["next_nonce"], PER_CHANNEL);
        }
    }
}
