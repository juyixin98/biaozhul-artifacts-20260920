//! HTTP 层端到端测试：直接对 axum Router 发请求，不监听真实端口。

use std::sync::Arc;

use axum::body::Body;
use axum::Router;
use disk_bptree::{build_router, AppState, BpTree};
use http::{Method, Request, StatusCode};
use tempfile::TempDir;
use tower::ServiceExt;

fn test_app(page_size: u16) -> (TempDir, Router) {
    let dir = TempDir::new().unwrap();
    let tree = BpTree::create(dir.path().join("http.db"), page_size).unwrap();
    (dir, build_router(Arc::new(AppState::new(tree))))
}

async fn send(
    app: &Router,
    method: Method,
    uri: &str,
    body: Option<&str>,
) -> (StatusCode, serde_json::Value) {
    let builder = Request::builder().method(method).uri(uri);
    let req = match body {
        Some(json) => builder
            .header("content-type", "application/json")
            .body(Body::from(json.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let json = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or_else(|_| {
            serde_json::json!({ "non_json_body": String::from_utf8_lossy(&bytes).into_owned() })
        })
    };
    (status, json)
}

#[tokio::test]
async fn full_lifecycle_over_http() {
    let (_dir, app) = test_app(64);

    // 健康检查。
    let (s, j) = send(&app, Method::GET, "/healthz", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["status"], "ok");

    // 初始未命中。
    let (s, j) = send(&app, Method::GET, "/keys/10", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["found"], false);
    assert!(j["value"].is_null());

    // 插入 → 201。
    let (s, j) = send(&app, Method::PUT, "/keys/10", Some(r#"{"value":100}"#)).await;
    assert_eq!(s, StatusCode::CREATED, "{j}");
    assert_eq!(j["inserted"], true);

    // 覆盖 → 200 + replaced。
    let (s, j) = send(&app, Method::PUT, "/keys/10", Some(r#"{"value":200}"#)).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["inserted"], false);
    assert_eq!(j["replaced"], 100);

    // 命中查询。
    let (s, j) = send(&app, Method::GET, "/keys/10", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["found"], true);
    assert_eq!(j["value"], 200);

    // 多插一些制造分裂。
    for k in 0..60i64 {
        let body = format!(r#"{{"value":{}}}"#, k * 2);
        let (s, _) = send(&app, Method::PUT, &format!("/keys/{k}"), Some(&body)).await;
        assert!(s == StatusCode::CREATED || s == StatusCode::OK);
    }

    // 范围读。
    let (s, j) = send(&app, Method::GET, "/range?start=5&end=9", None).await;
    assert_eq!(s, StatusCode::OK);
    let pairs = j["pairs"].as_array().unwrap();
    assert_eq!(pairs.len(), 5);
    assert_eq!(pairs[0]["key"], 5);
    assert_eq!(pairs[4]["key"], 9);
    assert_eq!(pairs[2]["value"], 14); // key 7 -> 14

    // 全量 + limit 截断。
    let (_, j) = send(&app, Method::GET, "/range", None).await;
    assert_eq!(j["pairs"].as_array().unwrap().len(), 60);
    let (_, j) = send(&app, Method::GET, "/range?limit=10", None).await;
    assert_eq!(j["pairs"].as_array().unwrap().len(), 10);
    assert_eq!(j["truncated"], true);

    // 删除。
    let (s, j) = send(&app, Method::DELETE, "/keys/10", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["deleted"], true);
    let (_, j) = send(&app, Method::DELETE, "/keys/10", None).await;
    assert_eq!(j["deleted"], false);
    let (_, j) = send(&app, Method::GET, "/keys/10", None).await;
    assert_eq!(j["found"], false);

    // stats / verify。
    let (s, j) = send(&app, Method::GET, "/stats", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(j["page_size"], 64);
    assert_eq!(j["leaf_capacity"], 3);
    assert_eq!(j["key_count"], 59);

    let (s, j) = send(&app, Method::POST, "/verify", None).await;
    assert_eq!(s, StatusCode::OK, "{j}");
    assert_eq!(j["ok"], true);
    assert!(j["height"].as_u64().unwrap() >= 2);
    assert!(j["leaf_fill_min"].as_f64().unwrap() >= 1.0 / 3.0 - 1e-9);
}

#[tokio::test]
async fn bad_requests_return_json_errors() {
    let (_dir, app) = test_app(256);
    let (s, j) = send(&app, Method::GET, "/keys/not-an-int", None).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert!(j["error"].is_string());

    let (s, _) = send(
        &app,
        Method::PUT,
        "/keys/1",
        Some(r#"{"not_value":1}"#),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);

    let (s, _) = send(&app, Method::GET, "/range?start=abc", None).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn verify_always_ok_through_random_http_ops() {
    let (_dir, app) = test_app(64);
    // 插入后删除到很少，触发借位/合并与降高。
    for k in 0..120i64 {
        send(
            &app,
            Method::PUT,
            &format!("/keys/{k}"),
            Some(&format!(r#"{{"value":{k}}}"#)),
        )
        .await;
    }
    let (s, j) = send(&app, Method::POST, "/verify", None).await;
    assert_eq!(s, StatusCode::OK, "{j}");
    assert!(j["height"].as_u64().unwrap() >= 2);

    // 删到只剩 1 键：根（内部节点）只剩一个孩子时坍缩降高，回到单层根叶。
    for k in 0..119i64 {
        send(&app, Method::DELETE, &format!("/keys/{k}"), None).await;
    }
    let (s, j) = send(&app, Method::POST, "/verify", None).await;
    assert_eq!(s, StatusCode::OK, "{j}");
    assert_eq!(j["height"], 1);
    assert_eq!(j["key_count"], 1);

    // 再删空 → 0 层，验证完整坍缩回空树。
    send(&app, Method::DELETE, "/keys/119", None).await;
    let (s, j) = send(&app, Method::POST, "/verify", None).await;
    assert_eq!(s, StatusCode::OK, "{j}");
    assert_eq!(j["height"], 0);
    assert_eq!(j["key_count"], 0);
}
