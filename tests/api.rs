//! 端到端集成测试：启动真实 Axum 服务（随机端口）+ 真实 HMAC-SHA256 签名 + 真实 SQLite。
//! 覆盖：鉴权、优先级、租约/TTL、急停锁存/解除、门控、查询不刷新租约、重启水合。

mod common;

use common::*;

#[test]
fn health_works() {
    let env = setup(None);
    let v: serde_json::Value = env.get_json("/health");
    assert_eq!(v["status"], "ok");
    assert_eq!(v["clock_mode"], "sim");
}

#[test]
fn unsigned_and_bad_signature_are_rejected() {
    let env = setup(None);
    // 无签名头
    let status = env.post_raw("/v1/sources/remote/commands", None, "{}");
    assert_eq!(status, 401);
    // 错误签名
    let status = env.post_raw("/v1/sources/remote/commands", Some("hex=00"), "{}");
    assert_eq!(status, 401);
    // 用自主密钥签遥控请求 → 拒绝
    let body = cmd_body(1, 1000, 10_000, 10_000, 1.0);
    let sig = sign(&env.keys.autonomous, "POST", "/v1/sources/remote/commands", "1000", &body);
    let status = env.post_raw(
        "/v1/sources/remote/commands",
        Some(&format!("hex={sig}")),
        &body,
    );
    assert_eq!(status, 401);
    // 篡改 body 后签名失效
    let body = cmd_body(1, 1000, 10_000, 10_000, 1.0);
    let sig = sign(&env.keys.remote, "POST", "/v1/sources/remote/commands", "1000", &body);
    let tampered = body.replacen("\"seq\":1", "\"seq\":2", 1);
    assert_ne!(body, tampered);
    let status = env.post_raw(
        "/v1/sources/remote/commands",
        Some(&format!("hex={sig}")),
        &tampered,
    );
    assert_eq!(status, 401);
}

#[test]
fn priority_lease_expiry_and_no_revive() {
    let env = setup(None);

    // 自主先跑
    let d = env.post_autonomous(1, 1000, 9000, 20000, 1.5, 1000);
    assert_eq!(d["decision"]["selected"]["source"], "autonomous");

    // 遥控接管（租约到 5000）
    let d = env.post_remote(1, 2000, 3000, 20000, 0.5, 2000);
    assert_eq!(d["decision"]["selected"]["source"], "remote");
    assert_eq!(
        d["decision"]["suppressed"][0]["reason"],
        "stale_after_override"
    );

    // GET 查询不刷新租约：一路查到 5001
    let d = env.get_json("/v1/decision?at=4000");
    assert_eq!(d["selected"]["source"], "remote");
    let d = env.get_json("/v1/decision?at=5001");
    assert!(d["selected"].is_null());
    assert_eq!(d["stop_reason"], "no_eligible_command");
    // 旧自主不许恢复
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["source"] == "autonomous" && s["reason"] == "stale_after_override"));

    // 旧 seq 重放被拒
    let resp = env.post_remote_expect_err(1, 5002, 3000, 20000, 0.5, 5002);
    assert_eq!(resp.0, 409);
    assert_eq!(resp.1["reason"], "stale_seq");

    // 到达即过期的消息被拒，且不推进 seq
    let resp = env.post_remote_expect_err(2, 1000, 10, 10, 0.5, 5002);
    assert_eq!(resp.1["reason"], "ttl_expired");

    // 新鲜自主恢复
    let d = env.post_autonomous(2, 5002, 1000, 20000, 1.5, 5002);
    assert_eq!(d["decision"]["selected"]["source"], "autonomous");
    assert_eq!(d["decision"]["selected"]["seq"], 2);
}

#[test]
fn estop_priority_latch_release_and_freshness_gate() {
    let env = setup(None);

    env.post_autonomous(1, 1000, 9000, 20000, 1.0, 1000);
    // 急停锁存
    let d = env.post_estop(1, "latch", 1500, None);
    assert_eq!(d["decision"]["stop_reason"], "estop_latched");
    assert!(d["decision"]["estop_latched"].as_bool().unwrap());
    assert_eq!(d["effective"], true);

    // 锁存期间 RC 命令被接受但抑制
    let d = env.post_remote(1, 1600, 5000, 20000, 0.0, 1600);
    assert_eq!(d["effective"], false);
    assert_eq!(d["decision"]["suppressed"].as_array().unwrap().len(), 2);

    // 锁存期间任何 GET 都是零速
    let d = env.get_json("/v1/decision?at=2000");
    assert!(d["selected"].is_null());
    assert_eq!(d["stop_reason"], "estop_latched");

    // 旧 estop seq 拒绝
    let (code, body) = env.post_estop_expect_err(1, "latch", 2000);
    assert_eq!(code, 409);
    assert_eq!(body["reason"], "stale_seq");

    // 解除
    let d = env.post_estop(2, "release", 3000, None);
    assert_eq!(d["effective"], true);

    // 解除后旧命令（锁存前自主 1000、锁存中 RC 1600）均不新鲜
    let d = env.get_json("/v1/decision?at=3001");
    assert!(d["selected"].is_null());
    let arr = d["suppressed"].as_array().unwrap();
    assert!(arr.iter().any(|s| s["source"] == "remote" && s["reason"] == "stale_after_estop_release"));
    assert!(arr.iter().any(|s| s["source"] == "autonomous" && s["reason"] == "stale_after_estop_release"));

    // 同刻（issued=3000）仍被挡
    env.post_remote(2, 3000, 1000, 20000, 0.0, 3002);
    let d = env.get_json("/v1/decision?at=3002");
    assert!(d["selected"].is_null());

    // 严格晚于解除时刻的新鲜 RC
    let d = env.post_remote(3, 3001, 1000, 20000, 0.7, 3003);
    assert_eq!(d["decision"]["selected"]["source"], "remote");
    assert_eq!(d["decision"]["output"]["vx"], 0.7);

    // 未锁存时 release 报错（seq 必须 > 3，否则先撞 stale_seq）
    let (code, body) = env.post_estop_expect_err(4, "release", 3100);
    assert_eq!(code, 409);
    assert_eq!(body["reason"], "estop_not_latched");
}

#[test]
fn ttl_shorter_than_lease_expires_first() {
    let env = setup(None);
    // lease=10000, ttl=500：占用在 1500 结束
    env.post_remote(1, 1000, 10_000, 500, 1.0, 1000);
    let d = env.get_json("/v1/decision?at=1499");
    assert_eq!(d["selected"]["source"], "remote");
    let d = env.get_json("/v1/decision?at=1500");
    assert!(d["selected"].is_null());
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["reason"] == "ttl_expired"));
}

#[test]
fn explicit_lease_release_requires_fresh_autonomy() {
    let env = setup(None);
    env.post_autonomous(1, 1000, 20_000, 30_000, 1.0, 1000);
    env.post_remote(1, 2000, 10_000, 30_000, 1.0, 2000);
    // RC 显式释放
    let d = env.post_release("remote", 2, "l1", 4000);
    assert_eq!(d["effective"], true);
    assert!(d["decision"]["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["source"] == "remote" && s["reason"] == "lease_released"));
    let d = env.get_json("/v1/decision?at=4000");
    assert!(d["selected"].is_null());
    // 释放后旧自主不复活，新鲜自主接手
    env.post_autonomous(2, 4001, 1000, 20000, 1.0, 4001);
    let d = env.get_json("/v1/decision?at=4001");
    assert_eq!(d["selected"]["source"], "autonomous");

    // lease_id 不匹配
    let (code, body) = env.post_release_expect_err("autonomous", 3, "nope", 4100);
    assert_eq!(code, 409);
    assert_eq!(body["reason"], "lease_mismatch");
}

#[test]
fn queries_and_ticks_do_not_refresh_leases() {
    let env = setup(None);
    env.post_remote(1, 1000, 1000, 10_000, 1.0, 1000); // 租约到 2000
    // 一堆 GET
    for at in [1200, 1500, 1800, 1999] {
        let d = env.get_json(&format!("/v1/decision?at={at}"));
        assert_eq!(d["selected"]["source"], "remote");
    }
    // 显式 tick（写决策记录但不续期）
    let d = env.post_tick(1999);
    assert_eq!(d["decision"]["selected"]["source"], "remote");
    // 租约仍在 2000 到期
    let d = env.get_json("/v1/decision?at=2000");
    assert!(d["selected"].is_null());
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["reason"] == "lease_expired"));

    // 决策历史只含 1 条命令 + 1 条 tick（GET 不留痕）
    let hist = env.get_json("/v1/decisions?limit=20");
    let kinds: Vec<&str> = hist["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["kind"].as_str().unwrap())
        .collect();
    assert_eq!(kinds.iter().filter(|k| **k == "evaluate").count(), 1);
    assert_eq!(kinds.iter().filter(|k| **k == "remote").count(), 1);
    assert!(!kinds.contains(&"query"));
}

#[test]
fn same_instant_contention_remote_wins_regardless_of_order() {
    for order in ["rc_first", "au_first"] {
        let env = setup(None);
        let send = || {
            if order == "rc_first" {
                env.post_remote(1, 5000, 5000, 10000, 1.0, 5000);
                env.post_autonomous(1, 5000, 5000, 10000, 1.0, 5000);
            } else {
                env.post_autonomous(1, 5000, 5000, 10000, 1.0, 5000);
                env.post_remote(1, 5000, 5000, 10000, 1.0, 5000);
            }
        };
        send();
        let d = env.get_json("/v1/decision?at=5000");
        assert_eq!(d["selected"]["source"], "remote", "order={order}");
        assert_eq!(d["suppressed"][0]["reason"], "stale_after_override");
    }
}

#[test]
fn clock_regression_rejected() {
    let env = setup(None);
    env.post_remote(1, 2000, 5000, 10000, 1.0, 2000);
    let (code, body) = env.post_remote_expect_err(2, 1000, 5000, 10000, 1.0, 1500);
    assert_eq!(code, 409);
    assert_eq!(body["reason"], "clock_regression");
}

#[test]
fn state_persists_across_restart() {
    let dir = tempdir();
    let db = db_path(&dir);

    {
        let env = setup(Some(&db));
        env.post_autonomous(1, 1000, 20_000, 30_000, 1.0, 1000);
        env.post_remote(1, 2000, 10_000, 30_000, 0.9, 2000);
        env.post_estop(1, "latch", 2500, Some("operator"));
        let d = env.get_json("/v1/decision?at=2500");
        assert_eq!(d["stop_reason"], "estop_latched");
    } // 服务关闭（drop shutdown）

    // 用同一份 DB 重启
    let env = setup(Some(&db));
    let d = env.get_json("/v1/decision?at=2500");
    assert_eq!(d["stop_reason"], "estop_latched");
    assert!(d["estop_latched"].as_bool().unwrap());

    let st = env.get_json("/v1/state");
    assert_eq!(st["estop"]["latched"], true);
    assert_eq!(st["estop"]["seq"], 1);
    assert_eq!(st["estop"]["reason"], "operator");
    assert_eq!(st["remote"]["last_seq"], 1);
    assert_eq!(st["autonomous"]["last_seq"], 1);

    // 重启后 seq 水位仍生效：重放旧 RC seq=1 被拒
    let (code, body) = env.post_remote_expect_err(1, 2000, 10000, 30000, 0.9, 2600);
    assert_eq!(code, 409);
    assert_eq!(body["reason"], "stale_seq");

    // 重启后解除急停，门控仍然有效
    env.post_estop(2, "release", 3000, None);
    let d = env.get_json("/v1/decision?at=3001");
    assert!(d["selected"].is_null());
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .all(|s| s["reason"] == "stale_after_estop_release"));

    // 新鲜 RC 恢复
    let d = env.post_remote(2, 3001, 1000, 20000, 0.5, 3001);
    assert_eq!(d["decision"]["selected"]["source"], "remote");

    // 决策历史跨重启保留
    let hist = env.get_json("/v1/decisions?limit=50");
    let n = hist["decisions"].as_array().unwrap().len();
    assert!(n >= 3, "history should survive restart, got {n}");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn decision_records_audit_every_write() {
    let env = setup(None);
    env.post_remote(1, 1000, 1000, 10000, 1.0, 1000);
    // 一条被拒消息
    env.post_remote_expect_err(1, 1000, 1000, 10000, 1.0, 1001);
    env.post_estop(1, "latch", 1200, None);

    let events = env.get_json("/v1/ingest-events?limit=10");
    let evs = events["events"].as_array().unwrap();
    assert!(evs.iter().any(|e| e["reason"] == "stale_seq" && e["accepted"] == false));

    let decisions = env.get_json("/v1/decisions?limit=10");
    let ds = decisions["decisions"].as_array().unwrap();
    // 接受的两条写入（remote + estop）各产生一条决策记录
    assert!(ds.iter().any(|d| d["kind"] == "remote" && d["accepted"] == true));
    assert!(ds.iter().any(|d| d["kind"] == "estop" && d["effective"] == true));
}
