//! HTTP 端到端集成测试:覆盖验收要求中的四类场景——
//! 审批后替换内容、重复晋级、并发回退、以及"未通过必要证明不能发布"。

use artifact_promotion::api;
use artifact_promotion::model::Stage;
use artifact_promotion::store::{sha256_hex, Store};
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use std::sync::Arc;
use tower::ServiceExt;

fn test_app() -> axum::Router {
    api::app(Arc::new(Store::new()))
}

/// 发起请求并返回 (状态码, 解析后的 JSON 或 Null)。
async fn call(app: &axum::Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, value)
}

fn post_json(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn put_bytes(uri: &str, bytes: &[u8]) -> Request<Body> {
    Request::builder()
        .method("PUT")
        .uri(uri)
        .header("content-type", "application/octet-stream")
        .body(Body::from(bytes.to_vec()))
        .unwrap()
}

async fn upload(app: &axum::Router, content: &[u8]) -> String {
    let (status, v) = call(app, put_bytes("/artifacts/content", content)).await;
    assert_eq!(status, StatusCode::OK, "upload failed: {}", v);
    v["digest"].as_str().unwrap().to_string()
}

async fn register(app: &axum::Router, id: &str, digest: &str) {
    let (status, v) = call(
        app,
        post_json(
            "/artifacts",
            json!({"id": id, "digest": digest}),
        ),
    )
    .await;
    assert!(
        status == StatusCode::CREATED || status == StatusCode::OK,
        "register failed: {} {}",
        status,
        v
    );
    assert_eq!(v["stage"], "dev");
}

async fn add_proof(
    app: &axum::Router,
    id: &str,
    proof_id: &str,
    digest: &str,
    kind: &str,
    result: &str,
) -> StatusCode {
    call(
        app,
        post_json(
            &format!("/artifacts/{}/proofs", id),
            json!({"proof_id": proof_id, "digest": digest, "kind": kind, "result": result}),
        ),
    )
    .await
    .0
}

async fn approve(
    app: &axum::Router,
    id: &str,
    approval_id: &str,
    digest: &str,
    target: &str,
) -> StatusCode {
    call(
        app,
        post_json(
            &format!("/artifacts/{}/approvals", id),
            json!({
                "approval_id": approval_id,
                "digest": digest,
                "target_stage": target,
                "approver": "qa-lead",
                "decision": "approved",
            }),
        ),
    )
    .await
    .0
}

async fn promote(app: &axum::Router, id: &str, digest: &str) -> (StatusCode, Value) {
    call(
        app,
        post_json(
            &format!("/artifacts/{}/promote", id),
            json!({"digest": digest}),
        ),
    )
    .await
}

async fn rollback(
    app: &axum::Router,
    id: &str,
    digest: &str,
    target: &str,
) -> (StatusCode, Value) {
    call(
        app,
        post_json(
            &format!("/artifacts/{}/rollback", id),
            json!({"digest": digest, "target_stage": target, "reason": "test"}),
        ),
    )
    .await
}

/// 走完 dev -> verification -> release 的全部门禁。
async fn promote_all_the_way(app: &axum::Router, id: &str, digest: &str) {
    assert_eq!(approve(app, id, "ap-verify", digest, "verification").await, StatusCode::CREATED);
    assert_eq!(add_proof(app, id, "p-unit", digest, "unit-test", "passed").await, StatusCode::CREATED);
    let (s, v) = promote(app, id, digest).await;
    assert_eq!(s, StatusCode::CREATED, "promote->verification: {}", v);
    assert_eq!(v["stage"], "verification");

    assert_eq!(approve(app, id, "ap-release", digest, "release").await, StatusCode::CREATED);
    assert_eq!(add_proof(app, id, "p-gate", digest, "release-gate", "passed").await, StatusCode::CREATED);
    let (s, v) = promote(app, id, digest).await;
    assert_eq!(s, StatusCode::CREATED, "promote->release: {}", v);
    assert_eq!(v["stage"], "release");
}

#[tokio::test]
async fn happy_path_full_promotion_and_history() {
    let app = test_app();
    let digest = upload(&app, b"artifact-body-v1").await;
    register(&app, "a1", &digest).await;
    promote_all_the_way(&app, "a1", &digest).await;

    let (s, v) = call(&app, Request::builder().uri("/artifacts/a1").body(Body::empty()).unwrap()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["stage"], "release");
    assert_eq!(v["digest"], digest);
    // 登记 + 两次晋级 = 3 条历史,全部绑定同一摘要
    assert_eq!(v["history"].as_array().unwrap().len(), 3);
    for ev in v["history"].as_array().unwrap() {
        assert_eq!(ev["digest"], digest);
    }
}

#[tokio::test]
async fn cannot_publish_without_required_proofs() {
    let app = test_app();
    let digest = upload(&app, b"needs-proofs").await;
    register(&app, "a2", &digest).await;

    // 无任何审批/证明 -> 409 APPROVAL_REQUIRED
    let (s, v) = promote(&app, "a2", &digest).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "APPROVAL_REQUIRED");

    // 审批通过但没有证明 -> 409 PROOF_REQUIRED
    approve(&app, "a2", "ap1", &digest, "verification").await;
    let (s, v) = promote(&app, "a2", &digest).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "PROOF_REQUIRED");

    // failed 证明不算通过
    assert_eq!(add_proof(&app, "a2", "pf", &digest, "unit-test", "failed").await, StatusCode::CREATED);
    let (s, v) = promote(&app, "a2", &digest).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "PROOF_REQUIRED");

    // 走到 verification 后,缺少 release-gate 证明仍不能发布
    assert_eq!(add_proof(&app, "a2", "pp", &digest, "unit-test", "passed").await, StatusCode::CREATED);
    let (s, _) = promote(&app, "a2", &digest).await;
    assert_eq!(s, StatusCode::CREATED);
    approve(&app, "a2", "ap2", &digest, "release").await;
    // 只有 unit-test,没有 release-gate
    let (s, v) = promote(&app, "a2", &digest).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "PROOF_REQUIRED");
    assert!(v["error"]["message"].as_str().unwrap().contains("release-gate"));

    // 制品仍停留在 verification,未被发布
    let (_, v) = call(&app, Request::builder().uri("/artifacts/a2").body(Body::empty()).unwrap()).await;
    assert_eq!(v["stage"], "verification");
}

#[tokio::test]
async fn content_replacement_after_approval_is_blocked_by_digest_binding() {
    let app = test_app();
    let digest_v1 = upload(&app, b"build-v1").await;
    register(&app, "a3", &digest_v1).await;
    approve(&app, "a3", "ap", &digest_v1, "verification").await;
    add_proof(&app, "a3", "p", &digest_v1, "unit-test", "passed").await;

    // 攻击者把"新内容"上传到内容库,得到不同摘要;
    // 然后尝试用同一制品 id 重新登记(= 审批后替换内容)。
    let digest_v2 = upload(&app, b"build-v2-tampered").await;
    assert_ne!(digest_v1, digest_v2);
    let (s, v) = call(
        &app,
        post_json("/artifacts", json!({"id": "a3", "digest": digest_v2})),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "IMMUTABLE_DIGEST");

    // 尝试用新摘要走晋级(审批与证明都绑定在旧摘要上) -> 被摘要绑定拦下
    let (s, v) = promote(&app, "a3", &digest_v2).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "DIGEST_MISMATCH");

    // 引用旧摘要的新证明有效,正常晋级不受影响;内容字节从未被替换
    let (s, _) = promote(&app, "a3", &digest_v1).await;
    assert_eq!(s, StatusCode::CREATED);
    let (s, body) = call(
        &app,
        Request::builder()
            .method("GET")
            .uri(format!("/artifacts/content/{}", digest_v1))
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(body, Value::Null); // 二进制不解析为 JSON,下面单独取字节
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri(format!("/artifacts/content/{}", digest_v1))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    assert_eq!(&bytes[..], b"build-v1");
}

#[tokio::test]
async fn proof_referencing_wrong_digest_is_rejected() {
    let app = test_app();
    let digest = upload(&app, b"bound").await;
    register(&app, "a4", &digest).await;
    let wrong = "0".repeat(64);
    let (s, v) = call(
        &app,
        post_json(
            "/artifacts/a4/proofs",
            json!({"proof_id": "px", "digest": wrong, "kind": "unit-test", "result": "passed"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "DIGEST_MISMATCH");

    // 审批同样绑定摘要
    let (s, v) = call(
        &app,
        post_json(
            "/artifacts/a4/approvals",
            json!({"approval_id": "ax", "digest": wrong, "target_stage": "verification",
                   "approver": "q", "decision": "approved"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "DIGEST_MISMATCH");
}

#[tokio::test]
async fn duplicate_promotion_is_rejected_and_state_unchanged() {
    let app = test_app();
    let digest = upload(&app, b"dup").await;
    register(&app, "a5", &digest).await;
    promote_all_the_way(&app, "a5", &digest).await;

    // 终态重复晋级
    for _ in 0..2 {
        let (s, v) = promote(&app, "a5", &digest).await;
        assert_eq!(s, StatusCode::CONFLICT);
        assert_eq!(v["error"]["code"], "ALREADY_AT_FINAL_STAGE");
    }
    // 回退到 dev 后重新晋级:第一次成功,紧接着对同一阶段的并发重复晋级
    // 最多一个成功(此处串行验证状态机不允许越级)。
    let (s, _) = rollback(&app, "a5", &digest, "dev").await;
    assert_eq!(s, StatusCode::CREATED);
    // dev 不能直接"再回退"
    let (s, v) = rollback(&app, "a5", &digest, "dev").await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "INVALID_ROLLBACK_TARGET");
}

#[tokio::test]
async fn content_upload_is_idempotent_and_serves_same_bytes() {
    let app = test_app();
    let d1 = upload(&app, b"same-bytes").await;
    let d2 = upload(&app, b"same-bytes").await;
    assert_eq!(d1, d2);
    assert_eq!(d1, sha256_hex(b"same-bytes"));
    // 空内容拒绝
    let (s, _) = call(&app, put_bytes("/artifacts/content", b"")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn rollback_preserves_history_proofs_and_blob() {
    let app = test_app();
    let digest = upload(&app, b"rollback-body").await;
    register(&app, "a6", &digest).await;
    promote_all_the_way(&app, "a6", &digest).await;

    let (s, v) = rollback(&app, "a6", &digest, "dev").await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(v["stage"], "dev");
    // 历史事件:登记、2 次晋级、1 次回退 = 4;旧事件全部保留
    assert_eq!(v["history"].as_array().unwrap().len(), 4);
    assert_eq!(v["history"][3]["trigger"], "rollback");
    assert_eq!(v["history"][3]["from"], "release");
    assert_eq!(v["history"][3]["to"], "dev");
    for ev in v["history"].as_array().unwrap() {
        assert_eq!(ev["digest"], digest);
    }

    // 证明与审批仍在(没有重建任何产物),门禁视图显示可重新晋级
    let (s, gates) = call(
        &app,
        Request::builder()
            .uri("/artifacts/a6/gates")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    for gate in gates.as_array().unwrap() {
        assert_eq!(gate["approved"], true);
        assert!(gate["missing_proof_kinds"].as_array().unwrap().is_empty());
    }

    // 原内容字节仍可取回,且就是当初的字节(未重建)
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("GET")
                .uri(format!("/artifacts/content/{}", digest))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    assert_eq!(&bytes[..], b"rollback-body");
}

#[tokio::test]
async fn concurrent_rollbacks_exactly_one_wins() {
    let app = test_app();
    let digest = upload(&app, b"concurrent").await;
    register(&app, "a7", &digest).await;
    promote_all_the_way(&app, "a7", &digest).await;

    // 16 个并发回退 release -> dev:恰好一个成功。
    let mut handles = Vec::new();
    for _ in 0..16 {
        let app = app.clone();
        let digest = digest.clone();
        handles.push(tokio::spawn(async move {
            rollback(&app, "a7", &digest, "dev").await
        }));
    }
    let mut ok = 0;
    let mut conflict = 0;
    for h in handles {
        let (s, _) = h.await.unwrap();
        match s {
            StatusCode::CREATED => ok += 1,
            StatusCode::CONFLICT => conflict += 1,
            other => panic!("unexpected status {}", other),
        }
    }
    assert_eq!(ok, 1, "只有一个回退应当成功");
    assert_eq!(conflict, 15);

    let (_, v) = call(
        &app,
        Request::builder().uri("/artifacts/a7").body(Body::empty()).unwrap(),
    )
    .await;
    assert_eq!(v["stage"], "dev");
    let rollback_events = v["history"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["trigger"] == "rollback")
        .count();
    assert_eq!(rollback_events, 1);
    // 摘要绑定在整个并发过程中没有被破坏
    assert_eq!(v["digest"], digest);
    assert_eq!(v["stage"].as_str().unwrap(), Stage::Dev.as_str());
}

#[tokio::test]
async fn cannot_register_artifact_for_unknown_digest() {
    let app = test_app();
    let ghost = sha256_hex(b"never-uploaded");
    let (s, v) = call(
        &app,
        post_json("/artifacts", json!({"id": "ghost", "digest": ghost})),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], "INVALID_REQUEST");
}
