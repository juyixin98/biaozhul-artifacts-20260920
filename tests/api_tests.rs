//! 端到端 HTTP 集成测试：直接对 Router 发 in-process 请求，
//! 使用注入时钟精确控制超时，使用独立临时事件日志验证持久化恢复。

use axum::body::Body;
use axum::http::{Request, StatusCode};
use quota_reservation::support::test_support::{make_app, send, TestApp};
use serde_json::json;
use tower::ServiceExt;

/// 基本生命周期：预留 -> 提交转实占 -> 再取消/再提交均被拒绝。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn reserve_commit_lifecycle() {
    let app = make_app(1_000, 100);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000,"quota_objects":100}))).await;

    let (sc, body) = send(
        &app,
        "POST",
        "/tenants/t1/reservations",
        Some(json!({"size_bytes":300,"objects":3,"ttl_ms":10000})),
    )
    .await;
    assert_eq!(sc, StatusCode::CREATED);
    let rid = body["reservation"]["reservation_id"].as_str().unwrap().to_string();
    assert_eq!(body["reservation"]["status"], "held");
    assert_eq!(body["created"], true);

    // 预留计入 held，不计入 committed。
    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 300);
    assert_eq!(t["held_objects"], 3);
    assert_eq!(t["committed_bytes"], 0);
    assert_eq!(t["active_reservations"], 1);

    // 提交：held -> committed。
    let (sc, c) = send(&app, "POST", &format!("/tenants/t1/reservations/{rid}/commit"), None).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(c["status"], "committed");

    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 0);
    assert_eq!(t["committed_bytes"], 300);
    assert_eq!(t["committed_objects"], 3);

    // 已提交不能取消（409），重复提交幂等返回同一记录。
    let (sc, _) = send(&app, "POST", &format!("/tenants/t1/reservations/{rid}/cancel"), None).await;
    assert_eq!(sc, StatusCode::CONFLICT);
    let (sc, c2) = send(&app, "POST", &format!("/tenants/t1/reservations/{rid}/commit"), None).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(c2["status"], "committed");
}

/// 取消释放预留额度；重复取消不多释放（released=false，状态保持）。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn cancel_releases_and_is_idempotent() {
    let app = make_app(500, 50);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":500,"quota_objects":50}))).await;

    let (_, body) = send(
        &app,
        "POST",
        "/tenants/t1/reservations",
        Some(json!({"size_bytes":500,"objects":50,"ttl_ms":10000})),
    )
    .await;
    let rid = body["reservation"]["reservation_id"].as_str().unwrap().to_string();

    // 满额：再来一个必然 429。
    let (sc, e) = send(
        &app,
        "POST",
        "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":1,"ttl_ms":10000})),
    )
    .await;
    assert_eq!(sc, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(e["error"], "quota_exceeded");

    // 取消后释放全部额度。
    let (sc, c) = send(&app, "POST", &format!("/tenants/t1/reservations/{rid}/cancel"), None).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(c["released"], true);
    assert_eq!(c["reservation"]["status"], "cancelled");

    // 现在可以再预留满额。
    let (sc, _) = send(
        &app,
        "POST",
        "/tenants/t1/reservations",
        Some(json!({"size_bytes":500,"objects":50,"ttl_ms":10000})),
    )
    .await;
    assert_eq!(sc, StatusCode::CREATED);

    // 重复取消：200 + released=false，不再次释放。
    let (sc, c2) = send(&app, "POST", &format!("/tenants/t1/reservations/{rid}/cancel"), None).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(c2["released"], false);
    assert_eq!(c2["reservation"]["status"], "cancelled");

    // 总额度仍被第二个预留占满：重复取消没有凭空释放。
    let (sc, _) = send(
        &app,
        "POST",
        "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":1,"ttl_ms":10000})),
    )
    .await;
    assert_eq!(sc, StatusCode::TOO_MANY_REQUESTS);
}

/// 验收核心：并发预留竞争同一剩余额度，成功数严格受配额约束。
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_reservations_never_oversubscribe() {
    let app = make_app(1_000, 100);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000,"quota_objects":100}))).await;

    // 20 个并发，每个 100 字节 / 1 对象：字节维度最多成功 10 个。
    let mut handles = Vec::new();
    for _ in 0..20 {
        let app = app.clone();
        handles.push(tokio::spawn(async move {
            let req = Request::builder()
                .method("POST")
                .uri("/tenants/t1/reservations")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"size_bytes":100,"objects":1,"ttl_ms":100000}).to_string(),
                ))
                .unwrap();
            let resp = app.oneshot(req).await.unwrap();
            resp.status()
        }));
    }

    let mut created = 0;
    let mut rejected = 0;
    for h in handles {
        match h.await.unwrap() {
            StatusCode::CREATED => created += 1,
            StatusCode::TOO_MANY_REQUESTS => rejected += 1,
            other => panic!("unexpected status {other}"),
        }
    }
    assert_eq!(created, 10, "exactly 10 of 20 concurrent reservations fit");
    assert_eq!(rejected, 10);

    // committed + held 永远不超配额。
    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 1000);
    assert_eq!(t["held_objects"], 10);
    assert!(t["held_bytes"].as_u64().unwrap() <= t["quota_bytes"].as_u64().unwrap());
    assert!(t["held_objects"].as_u64().unwrap() <= t["quota_objects"].as_u64().unwrap());
}

/// 验收核心：超时与提交/取消穿插。任何时刻 committed + held <= limit，
/// 过期后额度确实归还、可被新预留复用。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn timeout_interleaved_with_commit_and_cancel() {
    let app = make_app(1_000, 100);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000,"quota_objects":100}))).await;

    let reserve = |size: u64, objs: u64, ttl: i64| {
        let app = app.clone();
        async move {
            let (sc, body) = send(
                &app,
                "POST",
                "/tenants/t1/reservations",
                Some(json!({"size_bytes":size,"objects":objs,"ttl_ms":ttl})),
            )
            .await;
            let rid = body["reservation"]["reservation_id"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            (sc, rid)
        }
    };

    let (sc, a) = reserve(400, 4, 1000).await;
    assert_eq!(sc, StatusCode::CREATED);
    let (sc, _b) = reserve(300, 3, 2000).await;
    assert_eq!(sc, StatusCode::CREATED);
    let (sc, c) = reserve(300, 3, 5000).await;
    assert_eq!(sc, StatusCode::CREATED);

    // 1000 字节占满。
    let (sc, _) = reserve(1, 1, 5000).await;
    assert_eq!(sc, StatusCode::TOO_MANY_REQUESTS);

    // 推进 1000ms：a 到期（惰性/扫描释放）。
    let (sc, adv) = send(&app, "POST", "/admin/clock/advance", Some(json!({"delta_ms":1000}))).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(adv["expired"], 1);

    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 600);

    // a 已过期：提交 422，取消 422，且只释放过一次。
    let (sc, _) = send(&app, "POST", &format!("/tenants/t1/reservations/{a}/commit"), None).await;
    assert_eq!(sc, StatusCode::UNPROCESSABLE_ENTITY);
    let (sc, _) = send(&app, "POST", &format!("/tenants/t1/reservations/{a}/cancel"), None).await;
    assert_eq!(sc, StatusCode::UNPROCESSABLE_ENTITY);

    // 释放的 400 可复用：新预留 400 成功，401 失败（说明释放恰好 400，没多放）。
    let (sc, d) = reserve(400, 4, 5000).await;
    assert_eq!(sc, StatusCode::CREATED);
    let (sc, _) = reserve(1, 1, 5000).await;
    assert_eq!(sc, StatusCode::TOO_MANY_REQUESTS);

    // 再推进到 2000ms（总计）：b 到期；同时把 d 取消，穿插发生。
    let (_, _) = send(&app, "POST", "/admin/clock/advance", Some(json!({"delta_ms":1000}))).await;
    let (sc, _) = send(&app, "POST", &format!("/tenants/t1/reservations/{d}/cancel"), None).await;
    assert_eq!(sc, StatusCode::OK);

    // 只剩 c(300) 活跃，b 已过期、d 已取消。
    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 300);
    assert_eq!(t["held_objects"], 3);

    // 在超时边界之后提交 c（c ttl=5000，仍有效），转实占。
    let (sc, cc) = send(&app, "POST", &format!("/tenants/t1/reservations/{c}/commit"), None).await;
    assert_eq!(sc, StatusCode::OK);
    assert_eq!(cc["status"], "committed");

    // 提交过期预留：推进到 c 的到期点之后再试（c 已是 committed，不受影响）；
    // 另建一个短 TTL 预留验证过期提交。
    let (_, e) = reserve(100, 1, 1000).await;
    let (_, _) = send(&app, "POST", "/admin/clock/advance", Some(json!({"delta_ms":1001}))).await;
    let (sc, _) = send(&app, "POST", &format!("/tenants/t1/reservations/{e}/commit"), None).await;
    assert_eq!(sc, StatusCode::UNPROCESSABLE_ENTITY);

    // 最终不变量：committed + held <= quota（字节与对象数两维）。
    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    let committed_b = t["committed_bytes"].as_u64().unwrap();
    let held_b = t["held_bytes"].as_u64().unwrap();
    let committed_o = t["committed_objects"].as_u64().unwrap();
    let held_o = t["held_objects"].as_u64().unwrap();
    assert!(committed_b + held_b <= 1000, "bytes invariant: {committed_b}+{held_b}");
    assert!(committed_o + held_o <= 100, "objects invariant: {committed_o}+{held_o}");
    assert_eq!(committed_b, 300);
}

/// 幂等键：同一 (tenant, idempotency_key) 的并发/重复请求只产生一次预留。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn idempotency_key_single_reservation() {
    let app = make_app(1_000, 100);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000,"quota_objects":100}))).await;

    let mut handles = Vec::new();
    for _ in 0..5 {
        let app = app.clone();
        handles.push(tokio::spawn(async move {
            send(
                &app,
                "POST",
                "/tenants/t1/reservations",
                Some(json!({"size_bytes":900,"objects":9,"ttl_ms":100000,"idempotency_key":"k-upload-42"})),
            )
            .await
        }));
    }
    let mut rids = std::collections::HashSet::new();
    let mut created_count = 0;
    for h in handles {
        let (sc, body) = h.await.unwrap();
        assert!(
            sc == StatusCode::CREATED || sc == StatusCode::OK,
            "expected 201 (first) or 200 (replay), got {sc}"
        );
        rids.insert(body["reservation"]["reservation_id"].as_str().unwrap().to_string());
        if body["created"].as_bool().unwrap() {
            created_count += 1;
        }
    }
    // 并发下恰好一个请求先落盘（created=true, 201），其余命中幂等索引。
    assert_eq!(rids.len(), 1, "all responses reference one reservation");
    assert_eq!(created_count, 1, "reservation was created exactly once");

    let (_, t) = send(&app, "GET", "/tenants/t1", None).await;
    assert_eq!(t["held_bytes"], 900);
    assert_eq!(t["active_reservations"], 1);
}

/// 持久化：事件日志重放后，committed/held/各预留状态完全恢复。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn state_recovers_from_event_log() {
    let (r1, r2, r3) = {
        let app = TestApp::new();
        send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000,"quota_objects":100}))).await;
        let (_, b1) = send(&app, "POST", "/tenants/t1/reservations",
            Some(json!({"size_bytes":400,"objects":4,"ttl_ms":10000}))).await;
        let r1 = b1["reservation"]["reservation_id"].as_str().unwrap().to_string();
        let (_, b2) = send(&app, "POST", "/tenants/t1/reservations",
            Some(json!({"size_bytes":200,"objects":2,"ttl_ms":10000}))).await;
        let r2 = b2["reservation"]["reservation_id"].as_str().unwrap().to_string();
        let (_, b3) = send(&app, "POST", "/tenants/t1/reservations",
            Some(json!({"size_bytes":100,"objects":1,"ttl_ms":10000}))).await;
        let r3 = b3["reservation"]["reservation_id"].as_str().unwrap().to_string();
        // r1 提交、r2 取消，r3 保持 held。
        send(&app, "POST", &format!("/tenants/t1/reservations/{r1}/commit"), None).await;
        send(&app, "POST", &format!("/tenants/t1/reservations/{r2}/cancel"), None).await;

        // 模拟新进程：用同一事件日志副本重开。
        let app2 = app.reopen_copy(1_700_000_000_000);
        let (_, t) = send(&app2, "GET", "/tenants/t1", None).await;
        assert_eq!(t["quota_bytes"], 1000);
        assert_eq!(t["committed_bytes"], 400);
        assert_eq!(t["committed_objects"], 4);
        assert_eq!(t["held_bytes"], 100);
        assert_eq!(t["held_objects"], 1);
        assert_eq!(t["active_reservations"], 1);

        // 单条预留状态也被完整重放。
        let (sc, v1) = send(&app2, "GET", &format!("/tenants/t1/reservations/{r1}"), None).await;
        assert_eq!(sc, StatusCode::OK);
        assert_eq!(v1["status"], "committed");
        let (_, v2) = send(&app2, "GET", &format!("/tenants/t1/reservations/{r2}"), None).await;
        assert_eq!(v2["status"], "cancelled");
        let (_, v3) = send(&app2, "GET", &format!("/tenants/t1/reservations/{r3}"), None).await;
        assert_eq!(v3["status"], "held");

        // 已取消的不能再提交；held 的 r3 仍可提交（状态机重放后可继续运转）。
        let (sc, _) = send(&app2, "POST", &format!("/tenants/t1/reservations/{r2}/commit"), None).await;
        assert_eq!(sc, StatusCode::CONFLICT);
        let (sc, v3c) = send(&app2, "POST", &format!("/tenants/t1/reservations/{r3}/commit"), None).await;
        assert_eq!(sc, StatusCode::OK);
        assert_eq!(v3c["status"], "committed");
        let (_, t) = send(&app2, "GET", "/tenants/t1", None).await;
        assert_eq!(t["committed_bytes"], 500);
        assert_eq!(t["held_bytes"], 0);
        (r1, r2, r3)
    };
    // 确保 id 确实是服务端生成且互不相同（顺带覆盖了生成器）。
    assert_ne!(r1, r2);
    assert_ne!(r2, r3);
}

/// 对象数维度独立限制：字节没超但对象数超了也要拒绝。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn object_count_quota_enforced_separately() {
    let app = make_app(1_000_000, 2);
    send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":1000000,"quota_objects":2}))).await;
    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":10,"objects":2,"ttl_ms":10000}))).await;
    assert_eq!(sc, StatusCode::CREATED);
    let (sc, e) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":1,"ttl_ms":10000}))).await;
    assert_eq!(sc, StatusCode::TOO_MANY_REQUESTS);
    assert!(e["detail"].as_str().unwrap().contains("objects over: true"));
}

/// 非法请求返回 400；未知租户/预留返回 404。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn validation_and_not_found() {
    let app = make_app(1_000, 100);

    let (sc, _) = send(&app, "POST", "/tenants", Some(json!({"tenant_id":"","quota_bytes":1,"quota_objects":1}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);

    let (sc, _) = send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":100,"quota_objects":100}))).await;
    assert_eq!(sc, StatusCode::CREATED);
    let (sc, _) = send(&app, "POST", "/tenants", Some(json!({"tenant_id":"t1","quota_bytes":100,"quota_objects":100}))).await;
    assert_eq!(sc, StatusCode::CONFLICT);

    let (sc, _) = send(&app, "GET", "/tenants/nope", None).await;
    assert_eq!(sc, StatusCode::NOT_FOUND);

    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":0,"objects":1,"ttl_ms":10}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);
    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":0,"ttl_ms":10}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);
    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":1,"ttl_ms":0}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);
    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations",
        Some(json!({"size_bytes":1,"objects":1,"ttl_ms":10,"unknown_field":1}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);

    let (sc, _) = send(&app, "POST", "/tenants/t1/reservations/nope/commit", None).await;
    assert_eq!(sc, StatusCode::NOT_FOUND);
}

/// 系统时钟模式下推进时钟的管理接口不可用（400 clock_not_controllable）。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn system_clock_rejects_advance() {
    use quota_reservation::support::test_support::make_system_app;
    let app = make_system_app();
    let (sc, _) = send(&app, "POST", "/admin/clock/advance", Some(json!({"delta_ms":1}))).await;
    assert_eq!(sc, StatusCode::BAD_REQUEST);
}
