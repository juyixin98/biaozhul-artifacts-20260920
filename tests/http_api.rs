//! HTTP 级集成测试：通过 axum Router 的 oneshot 打完整请求/响应链路。

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use tower::util::ServiceExt;

use versioned_gas_meter::{
    api::{router, AppState},
    build_engine, CommittedState, Journal,
};

fn app() -> AppState {
    let state = Arc::new(CommittedState::memory());
    let journal = Arc::new(Journal::new(128));
    let engine = build_engine(state.clone(), journal.clone()).unwrap();
    AppState {
        engine,
        state,
        journal,
    }
}

async fn body_json(resp: axum::response::Response) -> serde_json::Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

fn b64(b: &[u8]) -> String {
    versioned_gas_meter::base64::encode(b)
}

async fn post_tx(app: AppState, payload: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let resp = router(app)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/transactions")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    (status, body_json(resp).await)
}

#[tokio::test]
async fn health_and_versions() {
    let resp = router(app())
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let resp = router(app())
        .oneshot(
            Request::builder()
                .uri("/v1/metering/versions")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let j = body_json(resp).await;
    assert_eq!(j["current"], 1);
    let versions = j["versions"].as_array().unwrap();
    assert_eq!(versions.len(), 2);
    assert_eq!(versions[0]["version"], 1);
    assert_eq!(versions[1]["version"], 2);
}

#[tokio::test]
async fn execute_sample_finite_loop_over_http() {
    let app_state = app();
    let payload = serde_json::json!({
        "module_ref": "sample:finite_loop",
        "input_base64": b64(&100_000u64.to_le_bytes()),
        "metering_version": 1,
    });
    let (status, j) = post_tx(app_state, payload).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(j["status"], "committed");
    assert!(j["termination"].is_null());
    assert!(j["fuel"]["consumed_total"].as_u64().unwrap() > 0);
}

#[tokio::test]
async fn terminated_execution_is_200_with_structured_reason() {
    // 沙箱按预期终止执行：HTTP 200（请求被受理），体中 status=terminated。
    let (status, j) = post_tx(
        app(),
        serde_json::json!({ "module_ref": "sample:infinite_loop" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(j["status"], "terminated");
    assert_eq!(j["termination"], "out_of_fuel");
    assert!(j["error"].as_str().unwrap().contains("fuel"));
    assert!(j["state_seq_after"].is_null());
}

#[tokio::test]
async fn denied_import_terminates_via_http() {
    let (status, j) = post_tx(
        app(),
        serde_json::json!({ "module_ref": "sample:denied_import" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(j["termination"], "link_error");
}

#[tokio::test]
async fn missing_module_is_400() {
    let (status, j) = post_tx(app(), serde_json::json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(j["code"], "bad_request");
}

#[tokio::test]
async fn invalid_base64_is_400() {
    let (status, _) = post_tx(
        app(),
        serde_json::json!({ "module_base64": "!!!not-base64!!!" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn unknown_metering_version_rejected() {
    let (status, j) = post_tx(
        app(),
        serde_json::json!({ "module_ref": "sample:oob_read", "metering_version": 99 }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "请求受理成功，但执行结果为失败");
    assert_eq!(j["termination"], "bad_request");
}

#[tokio::test]
async fn state_endpoint_reflects_only_commits_and_journal_records_all() {
    let app_state = app();

    // 一次成功提交。
    let (_, ok) = post_tx(
        app_state.clone(),
        serde_json::json!({ "module_ref": "sample:finite_loop", "input_base64": b64(&10u64.to_le_bytes()) }),
    )
    .await;
    assert_eq!(ok["status"], "committed");

    // 一次失败（陷阱）。
    let (_, bad) = post_tx(
        app_state.clone(),
        serde_json::json!({ "module_ref": "sample:trap_after_write" }),
    )
    .await;
    assert_eq!(bad["status"], "terminated");

    // /v1/state：只有 sum/hash，没有 poison。
    let resp = router(app_state.clone())
        .oneshot(
            Request::builder()
                .uri("/v1/state")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let state = body_json(resp).await;
    assert_eq!(state["state_seq"], 1);
    assert!(state["keys"]["sum"].is_number());
    assert!(state["keys"]["hash"].is_number());
    assert!(state["keys"]["poison"].is_null());

    // /v1/state?key=sum 能读值。
    let resp = router(app_state.clone())
        .oneshot(
            Request::builder()
                .uri("/v1/state?key=sum")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let one = body_json(resp).await;
    assert!(one["value_base64"].is_string());
    assert_eq!(one["size"].as_u64().unwrap(), 2); // "55" = 1..10 的和

    // /v1/journal：成功与失败各一条。
    let resp = router(app_state)
        .oneshot(
            Request::builder()
                .uri("/v1/journal?limit=10")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let journal = body_json(resp).await;
    let entries = journal["entries"].as_array().unwrap();
    assert_eq!(entries.len(), 2);
    let statuses: Vec<&str> = entries
        .iter()
        .map(|e| e["status"].as_str().unwrap())
        .collect();
    assert!(statuses.contains(&"committed"));
    assert!(statuses.contains(&"terminated"));
}

#[tokio::test]
async fn sample_download_endpoint_serves_wasm() {
    let resp = router(app())
        .oneshot(
            Request::builder()
                .uri("/v1/samples/finite_loop")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers()
            .get("content-type")
            .unwrap()
            .to_str()
            .unwrap(),
        "application/wasm"
    );
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&bytes[0..4], b"\0asm");
}

#[tokio::test]
async fn missing_version_path_is_404_and_samples_listed() {
    let resp = router(app())
        .oneshot(
            Request::builder()
                .uri("/v1/metering/versions/99")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    let resp = router(app())
        .oneshot(
            Request::builder()
                .uri("/v1/samples")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    let names: Vec<&str> = j["samples"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["name"].as_str().unwrap())
        .collect();
    for expected in [
        "finite_loop",
        "infinite_loop",
        "trap_after_write",
        "memory_grow",
        "oob_read",
        "host_bad_ptr",
        "denied_import",
    ] {
        assert!(names.contains(&expected), "missing sample {expected}");
    }
}
