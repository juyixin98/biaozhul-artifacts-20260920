//! 端到端集成测试：真实启动 axum 服务、真实 SQLite、手动可控时钟，
//! 覆盖租约到期、急停锁存/解除、同刻竞争、重启恢复、查询不刷租约、HMAC 鉴权等。
use motion_arbiter::test_support::*;

use serde_json::{json, Value};
use tempfile::TempDir;

struct Ctx {
    _dir: TempDir,
    db_path: String,
    cfg: serde_json::Value,
    port: u16,
    shutdown: Option<tokio::sync::oneshot::Sender<()>>,
}

impl Ctx {
    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{}", self.port, path)
    }
}

async fn spawn(seed_ms: i64) -> Ctx {
    let ctx = spawn_with_restart(None, seed_ms).await;
    wait_ready(&ctx).await;
    ctx
}

async fn respawn(old: Ctx, seed_ms: i64) -> Ctx {
    // 关掉旧进程，用同一个 db 文件重启（模拟“重启”验收）。
    old.shutdown.unwrap().send(()).ok();
    let mut ctx = spawn_with_restart(Some((old._dir, old.db_path, old.cfg)), seed_ms).await;
    wait_ready(&ctx).await;
    // 端口会变化：就绪后再返回。
    let _ = &mut ctx;
    ctx
}

async fn wait_ready(ctx: &Ctx) {
    for _ in 0..50 {
        if reqwest::Client::new()
            .get(ctx.url("/healthz"))
            .timeout(std::time::Duration::from_millis(100))
            .send()
            .await
            .is_ok()
        {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("测试服务未能在 2.5s 内就绪");
}

async fn spawn_with_restart(reuse: Option<(TempDir, String, Value)>, seed_ms: i64) -> Ctx {
    let (dir, db_path, cfg) = match reuse {
        Some(x) => x,
        None => {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("arbiter.db").display().to_string();
            let cfg = test_config();
            (dir, db_path, cfg)
        }
    };
    let cfg_path = dir.path().join("config.json");
    std::fs::write(&cfg_path, serde_json::to_vec(&cfg).unwrap()).unwrap();

    let state = build_test_state(&cfg_path.display().to_string(), &db_path, Some(seed_ms));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let app = build_test_router(state);
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                rx.await.ok();
            })
            .await
            .unwrap();
    });
    Ctx {
        _dir: dir,
        db_path,
        cfg,
        port,
        shutdown: Some(tx),
    }
}

fn key_of(cfg: &Value, source: &str) -> String {
    cfg["sources"][source]["key_hex"]
        .as_str()
        .unwrap()
        .to_string()
}

/// 构造一条签名的运动命令请求。
fn motion(ctx: &Ctx, source: &str, seq: i64, vx: f64, t: i64, lease: i64) -> SignedRequest {
    let body = json!({
        "seq": seq, "nonce": format!("{source}-n{seq}"),
        "vx": vx, "vy": 0.0, "omega": 0.0,
        "issue_ms": t, "valid_for_ms": 2000, "lease_for_ms": lease,
    });
    sign_motion(&key_of(&ctx.cfg, source), &body)
}

fn estop(ctx: &Ctx, seq: i64, action: &str, t: i64) -> SignedRequest {
    let body = json!({
        "seq": seq, "nonce": format!("estop-n{seq}-{action}"),
        "action": action, "issue_ms": t, "valid_for_ms": 2000,
    });
    sign_estop(&key_of(&ctx.cfg, "estop-1"), &body)
}

async fn post(ctx: &Ctx, path: &str, source: &str, req: &SignedRequest) -> (u16, Value) {
    let client = reqwest::Client::new();
    let resp = client
        .post(ctx.url(path))
        .header("X-Source", source)
        .header("X-Signature", format!("sha256={}", req.signature))
        .header("Content-Type", "application/json")
        .body(req.body_bytes.clone())
        .send()
        .await
        .unwrap();
    let status = resp.status().as_u16();
    let val = resp.json().await.unwrap_or(Value::Null);
    (status, val)
}

async fn advance(ctx: &Ctx, delta: i64) -> Value {
    reqwest::Client::new()
        .post(ctx.url("/admin/clock/advance"))
        .json(&json!({"delta_ms": delta}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap()
}

async fn get_json(ctx: &Ctx, path: &str) -> (u16, Value) {
    let resp = reqwest::get(ctx.url(path)).await.unwrap();
    let status = resp.status().as_u16();
    (status, resp.json().await.unwrap_or(Value::Null))
}

async fn evaluate(ctx: &Ctx, at: Option<i64>, persist: bool) -> Value {
    reqwest::Client::new()
        .post(ctx.url("/v1/evaluate"))
        .json(&json!({"at_ms": at, "persist": persist}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap()
}

fn chosen_source(d: &Value) -> String {
    d["chosen"]["source"].as_str().unwrap().to_string()
}
fn chosen_reason(d: &Value) -> String {
    d["chosen"]["reason"].as_str().unwrap().to_string()
}

const T0: i64 = 1_700_000_000_000;

#[tokio::test]
async fn priority_rc_over_autonomous_and_lease_expiry() {
    let ctx = spawn(T0).await;

    // 自主命令，租约足够长，确保遥控失联时它仍在自己的租约内（用于验证“不得恢复”）。
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.5, T0, 5000),
    )
    .await;
    assert_eq!(s, 202, "{r}");
    assert_eq!(chosen_source(&r["decision"]), "auto-1");

    // 遥控命令优先级更高，租期 1000ms。
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "rc-1",
        &motion(&ctx, "rc-1", 1, 1.0, T0, 1000),
    )
    .await;
    assert_eq!(s, 202);
    assert_eq!(chosen_source(&r["decision"]), "rc-1");
    // 自主被抑制，原因明确。
    assert!(r["decision"]["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x["source"] == "auto-1" && x["reason"] == "higher_priority_active"));

    // 时间推进 1001ms：租约到期。
    advance(&ctx, 1001).await;
    let d = evaluate(&ctx, None, false).await;
    // rc 失联，旧自主也在遥控窗口结束前接收 => 必须零速，不得恢复旧自主。
    assert_eq!(chosen_reason(&d), "no_active_command");
    assert_eq!(d["chosen"]["vx"], 0.0);
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| { x["source"] == "auto-1" && x["reason"] == "stale_after_remote_loss" }));
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| { x["source"] == "rc-1" && x["reason"] == "rc_lease_lost" }));
}

#[tokio::test]
async fn queries_must_not_refresh_leases() {
    let ctx = spawn(T0).await;
    let (_, _) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.5, T0, 500),
    )
    .await;

    // 在到期前做大量“查询类”请求。
    for _ in 0..5 {
        advance(&ctx, 200).await;
        get_json(&ctx, "/v1/decision/latest").await;
        get_json(&ctx, "/admin/state").await;
        get_json(&ctx, "/v1/decisions/history?limit=10").await;
        evaluate(&ctx, None, false).await; // persist=false
    }
    // 已推进 1000ms > 500ms 租期：租约必须仍然到期（查询没有续命）。
    let d = evaluate(&ctx, None, false).await;
    assert_eq!(chosen_reason(&d), "no_active_command");
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| { x["source"] == "auto-1" && x["reason"] == "lease_expired" }));

    // persist=true 的 evaluate 只新增“决策记录”，同样不改租约：再做一次仍然到期。
    let before = get_json(&ctx, "/admin/state").await.1["decision_records"].as_i64();
    evaluate(&ctx, None, true).await;
    let after = get_json(&ctx, "/admin/state").await.1["decision_records"].as_i64();
    assert_eq!(
        after.unwrap(),
        before.unwrap() + 1,
        "evaluate 只应新增决策记录"
    );
    let d = evaluate(&ctx, None, false).await;
    assert_eq!(chosen_reason(&d), "no_active_command");
}

#[tokio::test]
async fn stale_seq_expired_and_future_are_rejected() {
    let ctx = spawn(T0).await;
    // seq=1 正常。
    let (s, _) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.5, T0, 1000),
    )
    .await;
    assert_eq!(s, 202);
    // 旧 seq 拒绝。
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.5, T0, 1000),
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "stale_sequence");
    // 时间推进后，issue_ms 太早（过期）拒绝。
    advance(&ctx, 3000).await;
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 2, 0.5, T0 + 100, 1000),
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "expired");
    // issue_ms 过于超前拒绝。
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 3, 0.5, T0 + 100_000, 1000),
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "too_far_in_future");
    // 租约超长拒绝。
    let body = json!({"seq": 4, "nonce": "auto-1-n4", "vx": 0.1, "vy": 0.0, "omega": 0.0,
        "issue_ms": T0 + 3000, "valid_for_ms": 2000, "lease_for_ms": 999_999});
    let signed = sign_motion(&key_of(&ctx.cfg, "auto-1"), &body);
    let (s, r) = post(&ctx, "/v1/command", "auto-1", &signed).await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "lease_too_long");
}

#[tokio::test]
async fn auth_failures_are_rejected() {
    let ctx = spawn(T0).await;
    let req = motion(&ctx, "auto-1", 1, 0.5, T0, 1000);
    let client = reqwest::Client::new();

    // 错误签名。
    let resp = client
        .post(ctx.url("/v1/command"))
        .header("X-Source", "auto-1")
        .header("X-Signature", "sha256=deadbeef")
        .header("Content-Type", "application/json")
        .body(req.body_bytes.clone())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status().as_u16(), 401);
    let v: Value = resp.json().await.unwrap();
    assert_eq!(v["error"], "bad_signature");

    // 未知来源。
    let resp = client
        .post(ctx.url("/v1/command"))
        .header("X-Source", "mallory")
        .header("X-Signature", "sha256=deadbeef")
        .body(req.body_bytes.clone())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status().as_u16(), 401);
    assert_eq!(
        resp.json::<Value>().await.unwrap()["error"],
        "unknown_source"
    );

    // 运动签名不能用于急停接口（跨域）。
    let (s, r) = post(
        &ctx,
        "/v1/estop",
        "estop-1",
        &SignedRequest {
            body_bytes: req.body_bytes.clone(),
            signature: req.signature.clone(),
        },
    )
    .await;
    assert_eq!(s, 401);
    assert_eq!(r["error"], "bad_signature");
}

#[tokio::test]
async fn estop_latches_clear_needs_fresh_commands() {
    let ctx = spawn(T0).await;
    let (_, _) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.9, T0, 5000),
    )
    .await;
    let (_, _) = post(
        &ctx,
        "/v1/command",
        "rc-1",
        &motion(&ctx, "rc-1", 1, 1.0, T0, 5000),
    )
    .await;

    // 急停按下：零速锁存，优先级最高。
    let (s, r) = post(&ctx, "/v1/estop", "estop-1", &estop(&ctx, 1, "trigger", T0)).await;
    assert_eq!(s, 202);
    let d = &r["decision"];
    assert_eq!(d["estop_latched"], true);
    assert_eq!(chosen_source(d), "estop-1");
    assert_eq!(d["chosen"]["vx"], 0.0);
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .all(|x| x["reason"] == "suppressed_while_estop_latched"));

    // 时间推进，锁存仍然保持（不会自动解除）。
    advance(&ctx, 50_000).await;
    let d = evaluate(&ctx, None, false).await;
    assert_eq!(d["estop_latched"], true);
    assert_eq!(chosen_reason(&d), "estop_triggered");

    // 未锁存时 clear 必须报错（这里是锁存态，先验证正常 clear；重复 clear 在后面验证）。
    let (s, r) = post(
        &ctx,
        "/v1/estop",
        "estop-1",
        &estop(&ctx, 2, "clear", T0 + 50_000),
    )
    .await;
    assert_eq!(s, 202, "{r}");
    assert_eq!(r["decision"]["estop_latched"], false);

    // 解除后：所有急停事件前的旧命令失效，必须重新下发新鲜命令。
    let d = evaluate(&ctx, None, false).await;
    assert_eq!(chosen_reason(&d), "no_active_command");
    assert!(d["suppressed"]
        .as_array()
        .unwrap()
        .iter()
        .all(|x| { x["reason"] == "invalidated_by_estop_event" }));

    // 再次 clear（当前未锁存）=> 422 estop_not_active。
    let (s, r) = post(
        &ctx,
        "/v1/estop",
        "estop-1",
        &estop(&ctx, 3, "clear", T0 + 50_000),
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "estop_not_active");

    // 新鲜自主命令必须在 clear 事件接收时刻“之后”到达（严格新）。
    advance(&ctx, 100).await;
    let now = T0 + 50_100;
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 2, 0.6, now, 1000),
    )
    .await;
    assert_eq!(s, 202);
    assert_eq!(chosen_source(&r["decision"]), "auto-1");
}

#[tokio::test]
async fn same_instant_competition_is_deterministic() {
    let ctx = spawn(T0).await;
    // 两个自主来源、完全同一时刻、同租期、同序号。
    let a = motion(&ctx, "auto-1", 1, 1.0, T0, 1000);
    let b = motion(&ctx, "auto-b", 1, 1.0, T0, 1000);
    // 顺序不影响结果：先发 b 再发 a。
    let (_, _) = post(&ctx, "/v1/command", "auto-b", &b).await;
    let (_, _) = post(&ctx, "/v1/command", "auto-1", &a).await;

    let d = evaluate(&ctx, Some(T0 + 10), false).await;
    // 优先级相同 => 来源名字典序 "auto-1" < "auto-b"，确定性胜出。
    assert_eq!(chosen_source(&d), "auto-1");

    // 再来一个 rc 同刻命令：rc 必须稳定优先。
    let rc = motion(&ctx, "rc-1", 1, 1.0, T0, 1000);
    let (_, _) = post(&ctx, "/v1/command", "rc-1", &rc).await;
    let d = evaluate(&ctx, Some(T0 + 10), false).await;
    assert_eq!(chosen_source(&d), "rc-1");
}

#[tokio::test]
async fn same_instant_concurrent_requests_are_serialized() {
    let ctx = std::sync::Arc::new(spawn(T0).await);
    // 8 个来源同时刻并发投递；所有写入必须被接受且无 500/竞争错乱。
    let mut handles = Vec::new();
    for i in 0..8 {
        let ctx = ctx.clone();
        handles.push(tokio::spawn(async move {
            let source = format!("auto-{i}");
            let body = json!({
                "seq": 1, "nonce": format!("{source}-n1"), "vx": 0.1 * (i as f64),
                "vy": 0.0, "omega": 0.0,
                "issue_ms": T0, "valid_for_ms": 2000, "lease_for_ms": 1000,
            });
            let signed = sign_motion(&key_of(&ctx.cfg, &source), &body);
            post(&ctx, "/v1/command", &source, &signed).await
        }));
    }
    for h in handles {
        let (status, r) = h.await.unwrap();
        assert_eq!(status, 202, "并发投递必须全部成功: {r}");
    }
    // 无论投递完成顺序如何，同刻仲裁结果确定：auto-0 字典序最小。
    let d = evaluate(&ctx, Some(T0 + 5), false).await;
    assert_eq!(chosen_source(&d), "auto-0");
}

#[tokio::test]
async fn restart_preserves_state_and_manual_clock() {
    let ctx = spawn(T0).await;
    let (_, _) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.7, T0, 5000),
    )
    .await;
    let (_, _) = post(&ctx, "/v1/estop", "estop-1", &estop(&ctx, 1, "trigger", T0)).await;
    // 推进时钟并持久化。
    advance(&ctx, 12_345).await;
    let persisted_now = T0 + 12_345;

    // 重启：同一 db，不给 seed（应从 meta 恢复时钟）。
    let ctx = respawn(ctx, 0 /* ignored when meta exists */).await;
    let (_, time_v) = get_json(&ctx, "/v1/time").await;
    assert_eq!(time_v["mode"], "manual");
    assert_eq!(time_v["now_ms"], persisted_now);

    // 历史命令、急停锁存都恢复：仍处于急停锁存。
    let d = evaluate(&ctx, None, false).await;
    assert_eq!(d["estop_latched"], true);
    assert_eq!(chosen_reason(&d), "estop_triggered");

    // 旧 seq 在重启后依然拒绝（序号持久化）。
    let (s, r) = post(
        &ctx,
        "/v1/command",
        "auto-1",
        &motion(&ctx, "auto-1", 1, 0.7, persisted_now, 1000),
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(r["error"], "stale_sequence");

    // 决策历史也持久化（至少有一条急停触发时的决策记录）。
    let (s, hist) = get_json(&ctx, "/v1/decisions/history?limit=100").await;
    assert_eq!(s, 200);
    assert!(hist
        .as_array()
        .unwrap()
        .iter()
        .any(|d| { d["estop_latched"] == true && d["chosen"]["source"] == "estop-1" }));
}
