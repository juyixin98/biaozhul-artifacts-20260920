//! HTTP 端到端测试：通过 Axum router（不走真实端口）验证接口行为，
//! 覆盖二环、三环、相邻不重叠区间、锁升级四个验收场景及基本错误处理。

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use range_lock::engine::Engine;
use range_lock::http::router;
use serde_json::{json, Value};
use tower::util::ServiceExt;

fn app() -> axum::Router {
    router(Engine::new())
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    let body = match body {
        Some(v) => {
            builder = builder.header("content-type", "application/json");
            Body::from(v.to_string())
        }
        None => Body::empty(),
    };
    let resp = app
        .clone()
        .oneshot(builder.body(body).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let value: Value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, value)
}

async fn begin(app: &axum::Router, id: &str) {
    let (s, _) = call(app, "POST", &format!("/txn/{id}/begin"), None).await;
    assert_eq!(s, StatusCode::OK);
}

async fn lock(
    app: &axum::Router,
    id: &str,
    resource: &str,
    mode: &str,
    start: i64,
    end: i64,
) -> (StatusCode, Value) {
    call(
        app,
        "POST",
        &format!("/txn/{id}/locks"),
        Some(json!({ "resource": resource, "mode": mode, "start": start, "end": end })),
    )
    .await
}

#[tokio::test]
async fn health_and_lifecycle() {
    let app = app();
    let (s, v) = call(&app, "GET", "/health", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["status"], "ok");

    begin(&app, "t1").await;
    let (s, v) = lock(&app, "t1", "r", "shared", 0, 10).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["granted"], true);

    // 未 begin 的事务返回 404。
    let (s, _) = lock(&app, "ghost", "r", "shared", 0, 10).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    // 非法区间 400。
    let (s, _) = lock(&app, "t1", "r", "shared", 5, 5).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    // 非法模式 400。
    let (s, _) = lock(&app, "t1", "r", "weird", 0, 1).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    let (s, _) = call(&app, "POST", "/txn/t1/commit", None).await;
    assert_eq!(s, StatusCode::OK);
}

#[tokio::test]
async fn http_two_cycle() {
    // 二环：A 持 r1，B 持 r2；B 等 r1；A 请求 r2 成环，B 为最大 id 牺牲者，
    // A 当场获得 r2，随后 A 提交；B 的请求返回 409。
    let app = app();
    begin(&app, "A").await;
    begin(&app, "B").await;
    assert_eq!(lock(&app, "A", "r1", "exclusive", 0, 10).await.0, StatusCode::OK);
    assert_eq!(lock(&app, "B", "r2", "exclusive", 0, 10).await.0, StatusCode::OK);

    let (s, v) = lock(&app, "B", "r1", "exclusive", 0, 10).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["queued"], true);
    assert!(v["deadlock"].is_null());

    let (s, v) = lock(&app, "A", "r2", "exclusive", 0, 10).await;
    assert_eq!(s, StatusCode::OK, "A is not the victim");
    assert_eq!(v["granted"], true);
    assert_eq!(v["deadlock"]["victims"][0], "B");
    let cycles = &v["deadlock"]["cycles"][0];
    assert_eq!(cycles[0], "A");
    assert_eq!(cycles[1], "B");

    // B 已中止 => 409。
    let (s, _) = lock(&app, "B", "r3", "shared", 0, 10).await;
    assert_eq!(s, StatusCode::CONFLICT);

    // 等待图清空，r1 无主、r2 归 A。
    let (s, waits) = call(&app, "GET", "/waits", None).await;
    assert_eq!(s, StatusCode::OK);
    assert!(waits.as_array().unwrap().is_empty());
    let (_, res) = call(&app, "GET", "/resources", None).await;
    let r2 = res
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["resource"] == "r2")
        .unwrap();
    assert_eq!(r2["held"][0]["txn"], "A");

    let (s, _) = call(&app, "POST", "/txn/A/commit", None).await;
    assert_eq!(s, StatusCode::OK);
}

#[tokio::test]
async fn http_three_cycle() {
    // 三环 A->B->C->A，C 牺牲；C 释放 rC 后 B 获得，B 提交后 A 获得。
    let app = app();
    for t in ["A", "B", "C"] {
        begin(&app, t).await;
    }
    lock(&app, "A", "rA", "exclusive", 0, 10).await;
    lock(&app, "B", "rB", "exclusive", 0, 10).await;
    lock(&app, "C", "rC", "exclusive", 0, 10).await;
    assert_eq!(lock(&app, "A", "rB", "exclusive", 0, 10).await.1["queued"], true);
    assert_eq!(lock(&app, "B", "rC", "exclusive", 0, 10).await.1["queued"], true);

    let (s, v) = lock(&app, "C", "rA", "exclusive", 0, 10).await;
    // C 既是调用者也是牺牲者 => 409。
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["deadlock"]["victims"][0], "C");
    assert_eq!(v["granted"], false);
    assert_eq!(v["queued"], false);

    let (_, res) = call(&app, "GET", "/resources", None).await;
    let find = |name: &str| {
        res.as_array()
            .unwrap()
            .iter()
            .find(|r| r["resource"] == name)
            .unwrap()
            .clone()
    };
    assert_eq!(find("rC")["held"][0]["txn"], "B");
    assert_eq!(find("rA")["held"][0]["txn"], "A");
    // A 仍在等 rB。
    let (_, waits) = call(&app, "GET", "/waits", None).await;
    assert_eq!(waits.as_array().unwrap().len(), 1);
    assert_eq!(waits[0]["from"], "A");
    assert_eq!(waits[0]["to"], "B");

    call(&app, "POST", "/txn/B/commit", None).await;
    let (_, res) = call(&app, "GET", "/resources", None).await;
    let rb = res
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["resource"] == "rB")
        .unwrap();
    assert_eq!(rb["held"][0]["txn"], "A");
}

#[tokio::test]
async fn http_adjacent_ranges_no_false_positive() {
    let app = app();
    for t in ["A", "B", "C"] {
        begin(&app, t).await;
    }
    for (t, s, e) in [("A", 0, 10), ("B", 10, 20), ("C", 20, 30)] {
        let (st, v) = lock(&app, t, "r", "exclusive", s, e).await;
        assert_eq!(st, StatusCode::OK);
        assert_eq!(v["granted"], true);
    }
    let (_, waits) = call(&app, "GET", "/waits", None).await;
    assert!(waits.as_array().unwrap().is_empty(), "adjacent ranges must not create waits: {waits}");
}

#[tokio::test]
async fn http_upgrade() {
    let app = app();
    begin(&app, "A").await;
    begin(&app, "B").await;
    lock(&app, "A", "r", "shared", 0, 10).await;
    lock(&app, "B", "r", "shared", 0, 10).await;

    // A 升级 X：被 B 的 S 阻塞，入队。
    let (_, v) = lock(&app, "A", "r", "exclusive", 0, 10).await;
    assert_eq!(v["queued"], true);
    // B 也升级 X：互相等待 => 死锁，B 牺牲，A 升级兑现。
    let (s, v) = lock(&app, "B", "r", "exclusive", 0, 10).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["deadlock"]["victims"][0], "B");

    let (_, res) = call(&app, "GET", "/resources", None).await;
    let r = &res.as_array().unwrap()[0];
    assert_eq!(r["held"].as_array().unwrap().len(), 1);
    assert_eq!(r["held"][0]["txn"], "A");
    assert_eq!(r["held"][0]["mode"], "exclusive");
    assert!(r["waiting"].as_array().unwrap().is_empty());
}
