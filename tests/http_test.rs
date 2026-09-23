//! End-to-end HTTP tests driving the real Axum router via tower::ServiceExt::oneshot.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use cow_snapshot::base64;
use cow_snapshot::server::app;
use cow_snapshot::store::Store;
use tempfile::TempDir;
use tower::ServiceExt;

struct Ctx {
    _dir: TempDir,
    app: axum::Router,
}

fn ctx() -> Ctx {
    let dir = TempDir::new().unwrap();
    let store = Arc::new(Store::open(dir.path()).unwrap());
    Ctx {
        _dir: dir,
        app: app(store),
    }
}

async fn send(app: &axum::Router, req: Request<Body>) -> (StatusCode, serde_json::Value) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    let json = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes)
            .unwrap_or_else(|e| panic!("non-json: {e}: {}", String::from_utf8_lossy(&bytes)))
    };
    (status, json)
}

fn get(path: &str) -> Request<Body> {
    Request::builder().uri(path).body(Body::empty()).unwrap()
}

fn del(path: &str) -> Request<Body> {
    Request::builder()
        .method("DELETE")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}

fn post_json(path: &str, json: &serde_json::Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(json).unwrap()))
        .unwrap()
}

fn put_json(path: &str, json: &serde_json::Value) -> Request<Body> {
    Request::builder()
        .method("PUT")
        .uri(path)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(json).unwrap()))
        .unwrap()
}

#[tokio::test]
async fn full_lifecycle_over_http() {
    let c = ctx();

    // healthz returns plain text
    let resp = c.app.clone().oneshot(get("/healthz")).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = axum::body::to_bytes(resp.into_body(), 64).await.unwrap();
    assert_eq!(&body[..], b"ok\n");

    // create first branch without a parent
    let (st, j) = send(
        &c.app,
        post_json("/branches", &serde_json::json!({"name":"main"})),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["copied_pages"], 1);

    // write pages using JSON base64
    let payload = base64::encode(b"http-page-0");
    let (st, j) = send(
        &c.app,
        put_json(
            "/branches/main/pages/0",
            &serde_json::json!({"data_b64": payload}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["copied_pages"], 2);
    let new_root = j["root"].as_u64().unwrap();

    // raw octet-stream write also accepted
    let resp = c
        .app
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/branches/main/pages/1")
                .header("content-type", "application/octet-stream")
                .body(Body::from("raw-bytes-page1"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    // read back both encodings' data
    let (st, j) = send(&c.app, get("/branches/main/pages/0")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["present"], true);
    assert_eq!(
        base64::decode(j["data_b64"].as_str().unwrap()).unwrap(),
        b"http-page-0"
    );
    let (_, j) = send(&c.app, get("/branches/main/pages/1")).await;
    assert_eq!(
        base64::decode(j["data_b64"].as_str().unwrap()).unwrap(),
        b"raw-bytes-page1"
    );

    // empty slot
    let (st, j) = send(&c.app, get("/branches/main/pages/9")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["present"], false);

    // branch and observe sharing, then diverge over HTTP
    let (st, _) = send(
        &c.app,
        post_json(
            "/branches",
            &serde_json::json!({"name":"dev","parent":"main"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED);
    let (_, jm) = send(&c.app, get("/branches/main/pages/0")).await;
    let (_, jd) = send(&c.app, get("/branches/dev/pages/0")).await;
    assert_eq!(jm["page_id"], jd["page_id"], "physical page shared");

    let p2 = base64::encode(b"dev-diverged");
    let (st, _) = send(
        &c.app,
        put_json(
            "/branches/dev/pages/0",
            &serde_json::json!({"data_b64": p2}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED);
    let (_, jm) = send(&c.app, get("/branches/main/pages/0")).await;
    let (_, jd) = send(&c.app, get("/branches/dev/pages/0")).await;
    assert_ne!(jm["page_id"], jd["page_id"]);

    // stats reflect roots/shared leaves
    let (st, stats) = send(&c.app, get("/stats")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(stats["branches"].as_array().unwrap().len(), 2);
    assert!(stats["pages_minted_total"].as_u64().unwrap() >= new_root);

    // delete dev: main content intact
    let (st, _) = send(&c.app, del("/branches/dev")).await;
    assert_eq!(st, StatusCode::OK);
    let (_, j) = send(&c.app, get("/branches/main/pages/0")).await;
    assert_eq!(
        base64::decode(j["data_b64"].as_str().unwrap()).unwrap(),
        b"http-page-0"
    );

    // error mapping
    let (st, j) = send(&c.app, get("/branches/missing/pages/0")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert_eq!(j["error"], "not_found");
    let (st, _) = send(
        &c.app,
        post_json("/branches", &serde_json::json!({"name":"main"})),
    )
    .await;
    assert_eq!(st, StatusCode::CONFLICT);
}

#[tokio::test]
async fn fault_endpoint_arms_and_disarms() {
    let c = ctx();
    send(
        &c.app,
        post_json("/branches", &serde_json::json!({"name":"main"})),
    )
    .await;

    let (st, j) = send(
        &c.app,
        put_json("/fault", &serde_json::json!({"point":"before_manifest"})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{j}");
    let (_, j) = send(&c.app, get("/fault")).await;
    assert_eq!(j["fault"], "before_manifest");
    let (st, _) = send(&c.app, del("/fault")).await;
    assert_eq!(st, StatusCode::OK);
    let (_, j) = send(&c.app, get("/fault")).await;
    assert!(j["fault"].is_null());
}
