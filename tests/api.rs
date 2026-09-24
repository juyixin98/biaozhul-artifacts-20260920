//! 端到端测试：通过 axum 内存服务（oneshot）提交验收场景，
//! 断言总判定与每条策略检查点。

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::Value;
use tower::ServiceExt;

use provenance::api;
use provenance::fixture::{self, examples};

fn app() -> axum::Router {
    let bundle = Arc::new(fixture::FixtureBundle {
        dir: std::path::PathBuf::from("fixtures"),
        ..fixture::generate()
    });
    api::router(bundle)
}

async fn post_json(uri: &str, body: Value) -> (StatusCode, Value) {
    let resp = app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or_else(|e| {
        panic!(
            "响应不是 JSON: {e}; body={}",
            String::from_utf8_lossy(&bytes)
        )
    });
    (status, value)
}

fn check_status<'a>(report: &'a Value, id: &str) -> &'a str {
    report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == id)
        .unwrap_or_else(|| panic!("报告中缺少检查 {id}"))["status"]
        .as_str()
        .unwrap()
}

#[tokio::test]
async fn healthz_ok() {
    let resp = app()
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn scenario_01_valid_is_allowed() {
    let bundle = fixture::generate();
    let req = serde_json::to_value(examples::valid(&bundle)).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "ALLOWED");
    for id in [
        "digest_algorithms",
        "trusted_builder",
        "signing_key_binding",
        "source_commit",
        "material_origins",
        "materials_complete",
        "output_match",
        "signature",
    ] {
        assert_eq!(check_status(&report, id), "PASS", "检查 {id} 应通过");
    }
}

#[tokio::test]
async fn scenario_02_output_substitution_is_denied_by_output_match() {
    let bundle = fixture::generate();
    let req = serde_json::to_value(examples::output_substituted(&bundle)).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "DENY");
    // 证明本身合法：签名、构建器、材料全部通过……
    assert_eq!(check_status(&report, "signature"), "PASS");
    assert_eq!(check_status(&report, "trusted_builder"), "PASS");
    assert_eq!(check_status(&report, "materials_complete"), "PASS");
    // ……唯一拒绝原因是输出摘要比对。
    assert_eq!(check_status(&report, "output_match"), "FAIL");
    assert!(report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "output_match")
        .unwrap()["detail"]
        .as_str()
        .unwrap()
        .contains("输出被替换"));
}

#[tokio::test]
async fn scenario_03_missing_material_is_denied_by_materials_complete() {
    let bundle = fixture::generate();
    let req = serde_json::to_value(examples::material_missing(&bundle)).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "DENY");
    assert_eq!(check_status(&report, "signature"), "PASS");
    assert_eq!(check_status(&report, "output_match"), "PASS");
    let check = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "materials_complete")
        .unwrap();
    assert_eq!(check["status"], "FAIL");
    assert!(check["detail"].as_str().unwrap().contains("证明缺失材料"));
    assert!(check["detail"].as_str().unwrap().contains("acme-sdk-5.0"));
}

#[tokio::test]
async fn scenario_04_cross_builder_reuse_is_denied_by_key_binding() {
    let bundle = fixture::generate();
    let req = serde_json::to_value(examples::cross_builder_reuse(&bundle)).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "DENY");
    // rogue 密钥对同一主体的签名自洽——验签本身通过，
    // 但公钥与可信构建器 A 的注册公钥不匹配。
    assert_eq!(check_status(&report, "signature"), "PASS");
    assert_eq!(check_status(&report, "trusted_builder"), "PASS");
    let check = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "signing_key_binding")
        .unwrap();
    assert_eq!(check["status"], "FAIL");
    assert!(check["detail"]
        .as_str()
        .unwrap()
        .contains("跨构建器复用证明"));
}

#[tokio::test]
async fn rogue_build_fails_origin_identity_and_output_checks() {
    let bundle = fixture::generate();
    let req = serde_json::to_value(examples::rogue_build(&bundle)).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "DENY");
    assert_eq!(check_status(&report, "trusted_builder"), "FAIL");
    assert_eq!(check_status(&report, "signing_key_binding"), "FAIL");
    assert_eq!(check_status(&report, "material_origins"), "FAIL");
    assert_eq!(check_status(&report, "materials_complete"), "FAIL");
    // 后门外的材料来自 evil-cache。
    let origin = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "material_origins")
        .unwrap();
    assert!(origin["detail"]
        .as_str()
        .unwrap()
        .contains("evil-cache.example.net"));
}

#[tokio::test]
async fn tampered_envelope_signature_fails() {
    let bundle = fixture::generate();
    let mut req = examples::valid(&bundle);
    // 直接翻转签名字节（hex 中替换一对字符）。
    let sig = &mut req.envelope.signature_hex;
    sig.replace_range(0..2, if &sig[0..2] == "00" { "ff" } else { "00" });
    let req = serde_json::to_value(req).unwrap();
    let (status, report) = post_json("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "DENY");
    assert_eq!(check_status(&report, "signature"), "FAIL");
}

#[tokio::test]
async fn invalid_json_returns_400() {
    let resp = app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/verify")
                .header("content-type", "application/json")
                .body(Body::from("{ not json"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn fixture_sign_then_verify_roundtrip() {
    let bundle = Arc::new(fixture::generate());
    let payload = provenance::fixture::data::sample_statement();
    let body = serde_json::json!({
        "builder_id": provenance::fixture::data::TRUSTED_BUILDER_A,
        "payload": payload,
    });

    let app = api::router(bundle.clone());
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/fixture-sign")
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let envelope: Value = serde_json::from_slice(&bytes).unwrap();

    let verify_body = serde_json::json!({
        "envelope": envelope,
        "expected_source_commit": provenance::fixture::data::sample_source_commit(),
        "expected_materials": provenance::fixture::data::sample_materials(),
        "expected_output": provenance::fixture::data::output_of(
            provenance::fixture::data::GOOD_OUTPUT_CONTENT),
    });
    let (status, report) = post_json("/v1/verify", verify_body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(report["verdict"], "ALLOWED");
}
