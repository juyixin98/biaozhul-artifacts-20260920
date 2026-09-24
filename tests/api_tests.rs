// HTTP integration tests driving the real Axum router.

use std::collections::BTreeMap;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use license_propagation::api;
use license_propagation::model::AnalyzeRequest;
use tower::ServiceExt;

async fn post_json(path: &str, body: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let app = api::router();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json = serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null);
    (status, json)
}

#[tokio::test]
async fn health_ok() {
    let app = api::router();
    let resp = app
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn validate_expression_works() {
    let (s, v) = post_json(
        "/api/validate-expression",
        serde_json::json!({"expression": "MIT OR (Apache-2.0 AND GPL-2.0-only WITH Classpath-exception-2.0)"}),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["valid"], true);
    assert_eq!(v["alternatives"].as_array().unwrap().len(), 2);

    let (s, v) = post_json(
        "/api/validate-expression",
        serde_json::json!({"expression": "MIT OR"}),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["valid"], false);
    assert!(v["error"].is_string());
}

fn req_json() -> serde_json::Value {
    serde_json::json!({
        "packages": [
            {"id": "app", "spdx": "MIT OR GPL-2.0-only"},
            {"id": "lib", "spdx": "GPL-2.0-only"}
        ],
        "edges": [{"from": "app", "to": "lib", "link": "static"}],
        "policy": {
            "allowed_licenses": {
                "MIT": ["*", "static", "dynamic"],
                "GPL-2.0-only": ["*", "static", "dynamic"]
            }
        },
        "exhaustive": true
    })
}

#[tokio::test]
async fn analyze_returns_selection_and_exhaustive_agreement() {
    let (s, v) = post_json("/api/analyze", req_json()).await;
    assert_eq!(s, StatusCode::OK, "{:?}", v);
    assert_eq!(v["satisfiable"], true);
    assert_eq!(v["selection"][0]["chosen"]["expression"], "GPL-2.0-only");
    assert_eq!(v["selection"][1]["chosen"]["expression"], "GPL-2.0-only");
    assert_eq!(v["exhaustive"]["agrees_with_solver"], true);
    assert_eq!(v["exhaustive"]["feasible_assignments"], 1);
    assert_eq!(v["exhaustive"]["total_assignments_of_feasible_terms"], 2);
}

#[tokio::test]
async fn analyze_reports_conflict_and_path() {
    let body = serde_json::json!({
        "packages": [
            {"id": "app", "spdx": "MIT"},
            {"id": "lib", "spdx": "GPL-2.0-only"}
        ],
        "edges": [{"from": "app", "to": "lib", "link": "static"}],
        "policy": {
            "allowed_licenses": {
                "MIT": ["*", "static"],
                "GPL-2.0-only": ["*", "static"]
            }
        }
    });
    let (s, v) = post_json("/api/analyze", body).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["satisfiable"], false);
    let c = &v["conflicts"][0];
    assert_eq!(c["kind"], "copyleft");
    assert_eq!(c["source"], "lib");
    assert_eq!(c["target"], "app");
    assert_eq!(c["path"][0]["from"], "app");
    assert_eq!(c["path"][0]["to"], "lib");
}

#[tokio::test]
async fn analyze_reports_cycles() {
    let body = serde_json::json!({
        "packages": [
            {"id": "a", "spdx": "MIT"},
            {"id": "b", "spdx": "MIT"}
        ],
        "edges": [
            {"from": "a", "to": "b", "link": "static"},
            {"from": "b", "to": "a", "link": "static"}
        ],
        "policy": {"allowed_licenses": {"MIT": ["*", "static", "dynamic"]}}
    });
    let (s, v) = post_json("/api/analyze", body).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["cycles"].as_array().unwrap().len(), 1);
    assert_eq!(v["cycles"][0]["nodes"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn bad_inputs_return_400() {
    // malformed json
    let app = api::router();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/analyze")
                .header("content-type", "application/json")
                .body(Body::from("{ not json"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

    // dangling edge
    let body = serde_json::json!({
        "packages": [{"id": "a", "spdx": "MIT"}],
        "edges": [{"from": "a", "to": "ghost", "link": "static"}],
        "policy": {}
    });
    let (s, _) = post_json("/api/analyze", body).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // bad expression
    let body = serde_json::json!({
        "packages": [{"id": "a", "spdx": "MIT OR"}],
        "policy": {}
    });
    let (s, _) = post_json("/api/analyze", body).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // unknown field rejected (deny_unknown_fields)
    let body = serde_json::json!({
        "packages": [{"id": "a", "spdx": "MIT", "extra": 1}]
    });
    let (s, _) = post_json("/api/analyze", body).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[test]
fn request_model_deserializes_defaults() {
    let v: AnalyzeRequest = serde_json::from_value(serde_json::json!({
        "packages": []
    }))
    .unwrap();
    assert_eq!(v.exhaustive_limit, 100_000);
    let _: BTreeMap<String, String> = BTreeMap::new();
}
