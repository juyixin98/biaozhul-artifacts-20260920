//! HTTP-level tests: drive the Axum router in-process (no network).

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use tower::ServiceExt; // for `oneshot`

fn diamond_request() -> serde_json::Value {
    serde_json::json!({
        "registry": { "packages": [
            { "name": "a", "versions": [
                { "version": "1.0.0", "dependencies": [
                    { "name": "c", "range": "^1.0.0" } ] } ] },
            { "name": "b", "versions": [
                { "version": "1.0.0", "dependencies": [
                    { "name": "c", "range": ">=1.0.0, <2.0.0" } ] } ] },
            { "name": "c", "versions": [
                { "version": "1.0.0" }, { "version": "1.5.0" }, { "version": "2.0.0" } ] }
        ] },
        "requirements": [
            { "name": "a", "range": "^1.0.0" },
            { "name": "b", "range": "^1.0.0" }
        ],
        "platform": "linux"
    })
}

async fn post(app: &axum::Router, path: &str, body: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let req = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let parsed = serde_json::from_slice(&bytes)
        .unwrap_or_else(|e| panic!("POST {path} -> {status}, body not JSON ({e}): {}", String::from_utf8_lossy(&bytes)));
    (status, parsed)
}

#[tokio::test]
async fn health_endpoint() {
    let app = depsolver::build_router();
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
}

#[tokio::test]
async fn resolve_and_replay_roundtrip() {
    let app = depsolver::build_router();

    let (status, body) = post(&app, "/resolve", diamond_request()).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "solved");
    assert_eq!(body["locked"]["c"], "1.5.0");
    assert!(body["lockfile"]["locked"].is_object());

    // Replay the returned lockfile: must be valid.
    let lockfile = body["lockfile"].clone();
    let replay_body = serde_json::json!({
        "registry": diamond_request()["registry"],
        "lockfile": lockfile,
    });
    let (status, body) = post(&app, "/replay", replay_body).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["valid"], true);

    // Tamper with the lock: c@2.0.0 violates b's "<2.0.0".
    let mut tampered = lockfile.clone();
    tampered["locked"]["c"] = serde_json::json!("2.0.0");
    let replay_body = serde_json::json!({
        "registry": diamond_request()["registry"],
        "lockfile": tampered,
    });
    let (status, body) = post(&app, "/replay", replay_body).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
    assert_eq!(body["valid"], false);
    assert!(!body["errors"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn resolve_unsolvable_returns_conflict_chain() {
    let app = depsolver::build_router();
    let mut req_body = diamond_request();
    // Make ranges mutually exclusive: a needs c>=2, b needs c<2.
    req_body["registry"]["packages"][0]["versions"][0]["dependencies"][0]["range"] =
        serde_json::json!(">=2.0.0");
    let (status, body) = post(&app, "/resolve", req_body).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "unsolvable");
    assert_eq!(body["conflict"]["package"], "c");
    let explanation = body["conflict"]["explanation"].as_str().unwrap();
    assert!(explanation.contains(">=2.0.0"), "{explanation}");
    assert!(explanation.contains("<2.0.0"), "{explanation}");
}

#[tokio::test]
async fn invalid_range_is_a_400() {
    let app = depsolver::build_router();
    let mut req_body = diamond_request();
    req_body["requirements"][0]["range"] = serde_json::json!("not-a-range");
    let (status, body) = post(&app, "/resolve", req_body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert!(body["error"].as_str().unwrap().contains("not-a-range"));
}
