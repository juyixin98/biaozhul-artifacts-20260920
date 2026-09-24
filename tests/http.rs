//! HTTP 层冒烟测试：真实走一遍 axum 路由 + JSON 序列化。

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use incr_build_planner::api;
use tower::ServiceExt; // for oneshot

async fn post(path: &str, body: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let app = api::app();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let json = serde_json::from_slice(&bytes).unwrap_or(serde_json::json!({}));
    (status, json)
}

#[tokio::test]
async fn health_ok() {
    let app = api::app();
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
}

#[tokio::test]
async fn plan_cold_build_http() {
    let body = serde_json::json!({
        "graph": {
            "nodes": [
                {"id": "a.in", "kind": "file"},
                {"id": "build_a", "kind": "build", "rule": {
                    "command": "cc a.in", "tool": "cc", "tool_version": "1",
                    "env": {"OPT": "2"}
                }},
                {"id": "a.out", "kind": "file"}
            ],
            "edges": [
                {"from": "a.in", "to": "build_a"},
                {"from": "build_a", "to": "a.out"}
            ]
        },
        "current": {
            "files": {"a.in": {"content_hash": "h1", "mtime_ms": 1}}
        }
    });
    let (status, json) = post("/plan", body).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["cold_build"], true);
    assert_eq!(json["steps"], serde_json::json!(["build_a"]));
    assert_eq!(json["full_order"], serde_json::json!(["build_a"]));
    assert!(json["builds"]["build_a"]["cache_key"]
        .as_str()
        .unwrap()
        .starts_with("sha256:"));
}

#[tokio::test]
async fn touch_only_returns_empty_steps_over_http() {
    let graph = serde_json::json!({
        "nodes": [
            {"id": "a.in", "kind": "file"},
            {"id": "build_a", "kind": "build", "rule": {"command": "cc a.in"}}
        ],
        "edges": [{"from": "a.in", "to": "build_a"}]
    });
    let previous = serde_json::json!({
        "files": {"a.in": {"content_hash": "h1", "mtime_ms": 1}},
        "build_keys": {} // 让 build_a 走 CacheMiss 不合适——这里给它真实键
    });
    // 先冷规划拿到键
    let cold = serde_json::json!({
        "graph": graph,
        "current": {"files": {"a.in": {"content_hash": "h1", "mtime_ms": 1}}}
    });
    let (_, cold_json) = post("/plan", cold).await;
    let key = cold_json["builds"]["build_a"]["cache_key"].clone();

    let body = serde_json::json!({
        "graph": graph,
        "previous": {
            "files": {"a.in": {"content_hash": "h1", "mtime_ms": 1}},
            "build_keys": {"build_a": key}
        },
        "current": {"files": {"a.in": {"content_hash": "h1", "mtime_ms": 9999}}}
    });
    let (status, json) = post("/plan", body).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["steps"], serde_json::json!([]));
    assert_eq!(json["timestamp_only_files"], serde_json::json!(["a.in"]));
    let _ = previous;
}

#[tokio::test]
async fn cycle_returns_422_with_location() {
    let body = serde_json::json!({
        "graph": {
            "nodes": [
                {"id": "x", "kind": "build", "rule": {"command": "1"}},
                {"id": "y", "kind": "build", "rule": {"command": "2"}},
                {"id": "z", "kind": "build", "rule": {"command": "3"}}
            ],
            "edges": [
                {"from": "x", "to": "y"},
                {"from": "y", "to": "z"},
                {"from": "z", "to": "x"}
            ]
        },
        "current": {}
    });
    let (status, json) = post("/plan", body).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(json["error"].as_str().unwrap().contains("cycle"));
    assert_eq!(json["cycles"][0]["nodes"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn validate_reports_cycle_but_not_500() {
    let body = serde_json::json!({
        "graph": {
            "nodes": [
                {"id": "x", "kind": "build", "rule": {"command": "1"}},
                {"id": "y", "kind": "build", "rule": {"command": "2"}}
            ],
            "edges": [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}]
        }
    });
    let (status, json) = post("/validate", body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["valid"], false);
    assert_eq!(json["cycles"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn malformed_json_is_400_not_500() {
    let app = api::app();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/plan")
                .header("content-type", "application/json")
                .body(Body::from("{ not json"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}
