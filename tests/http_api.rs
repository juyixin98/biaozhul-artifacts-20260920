//! HTTP 层集成测试：完整走 Axum 路由，时钟仍可注入，超时行为可确定。

use std::sync::Arc;

use axum::{
    body::Body,
    http::{Request, StatusCode},
    Router,
};
use quota_reserve::clock::FakeClock;
use quota_reserve::http::{router, AppState};
use quota_reserve::store::Store;
use serde_json::Value;
use tower::util::ServiceExt;

const T0: i64 = 1_700_000_000_000;

fn app() -> (Router, Arc<FakeClock>) {
    let store = Store::in_memory().unwrap();
    let clock = Arc::new(FakeClock::new(T0));
    let state = AppState { store, clock: clock.clone() as Arc<dyn quota_reserve::clock::Clock> };
    (router(state), clock)
}

async fn send(app: Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let value: Value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, value)
}

fn put_json(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("PUT")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn post_json(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn get(uri: &str) -> Request<Body> {
    Request::builder().method("GET").uri(uri).body(Body::empty()).unwrap()
}

#[tokio::test]
async fn full_lifecycle_over_http() {
    let (app, clock) = app();

    // 创建租户
    let (st, _) = send(
        app.clone(),
        put_json("/tenants/acme", serde_json::json!({"byte_quota": 1000, "object_quota": 10})),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED);

    // 预留 600 / 6
    let (st, body) = send(
        app.clone(),
        post_json(
            "/tenants/acme/reservations",
            serde_json::json!({"byte_size": 600, "object_count": 6, "ttl_ms": 1000}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED);
    assert_eq!(body["expires_at"], T0 + 1000);
    let id1 = body["reservation_id"].as_str().unwrap().to_string();

    // 再预留 500 / 5：字节与对象同时超限 -> 409
    let (st, body) = send(
        app.clone(),
        post_json(
            "/tenants/acme/reservations",
            serde_json::json!({"byte_size": 500, "object_count": 5, "ttl_ms": 1000}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CONFLICT);
    assert_eq!(body["error"], "quota_exceeded");
    assert_eq!(body["detail"]["bytes"]["used"], 600);

    // 用量
    let (st, body) = send(app.clone(), get("/tenants/acme/usage")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(body["reserved_bytes"], 600);
    assert_eq!(body["total_bytes"], 600);

    // 提交
    let (st, body) = send(app.clone(), post_json(&format!("/reservations/{id1}/commit"), Value::Null)).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(body["status"], "committed");

    // 提交后预留归零、实占 600
    let (_, body) = send(app.clone(), get("/tenants/acme/usage")).await;
    assert_eq!(body["reserved_bytes"], 0);
    assert_eq!(body["committed_bytes"], 600);

    // 已提交再取消 -> 409
    let (st, _) = send(app.clone(), post_json(&format!("/reservations/{id1}/cancel"), Value::Null)).await;
    assert_eq!(st, StatusCode::CONFLICT);

    // 超时路径：预留一笔短 TTL，拨钟
    let (_, body) = send(
        app.clone(),
        post_json(
            "/tenants/acme/reservations",
            serde_json::json!({"byte_size": 400, "object_count": 4, "ttl_ms": 500}),
        ),
    )
    .await;
    let id2 = body["reservation_id"].as_str().unwrap().to_string();

    clock.advance(501);
    // 主动扫描
    let (st, body) = send(app.clone(), post_json("/admin/sweep", Value::Null)).await;
    assert_eq!(st, StatusCode::OK);
    assert!(body["expired_count"].as_u64().unwrap() >= 1);

    // 到期后提交 -> 410
    let (st, _) = send(app.clone(), post_json(&format!("/reservations/{id2}/commit"), Value::Null)).await;
    assert_eq!(st, StatusCode::GONE);

    // 到期额度已释放：总量只剩实占 600
    let (_, body) = send(app.clone(), get("/tenants/acme/usage")).await;
    assert_eq!(body["total_bytes"], 600);
    assert_eq!(body["total_objects"], 6);

    // 未知预留 -> 404；未知租户用量 -> 404
    let (st, _) = send(app.clone(), post_json("/reservations/does-not-exist/commit", Value::Null)).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let (st, _) = send(app.clone(), get("/tenants/ghost/usage")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn double_cancel_over_http_is_idempotent() {
    let (app, _clock) = app();
    send(
        app.clone(),
        put_json("/tenants/acme", serde_json::json!({"byte_quota": 1000, "object_quota": 10})),
    )
    .await;
    let (_, body) = send(
        app.clone(),
        post_json(
            "/tenants/acme/reservations",
            serde_json::json!({"byte_size": 1000, "object_count": 10, "ttl_ms": 10000}),
        ),
    )
    .await;
    let id = body["reservation_id"].as_str().unwrap().to_string();

    for _ in 0..2 {
        let (st, body) =
            send(app.clone(), post_json(&format!("/reservations/{id}/cancel"), Value::Null)).await;
        assert_eq!(st, StatusCode::OK);
        assert_eq!(body["status"], "cancelled");
    }
    let (_, body) = send(app.clone(), get("/tenants/acme/usage")).await;
    assert_eq!(body["total_bytes"], 0);
    assert_eq!(body["total_objects"], 0);
}
