//! 端到端 HTTP 集成测试（通过 oneshot 直连 Router，无需监听端口）。

use std::collections::HashSet;

use artifact_promotion::app::{app, AppState};
use axum::body::Body;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::Router;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

fn router() -> Router {
    app(AppState::new())
}

fn digest_of(content: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(content)))
}

async fn send(router: &Router, req: Request<Body>) -> (StatusCode, Vec<u8>, HeaderMap) {
    let resp = router.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, bytes, headers)
}

fn post_json(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

async fn register(router: &Router, content: &[u8]) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri("/artifacts")
        .header("content-type", "application/octet-stream")
        .body(Body::from(content.to_vec()))
        .unwrap();
    let (s, b, _) = send(router, req).await;
    (s, serde_json::from_slice(&b).unwrap())
}

async fn get_json(router: &Router, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder().uri(uri).body(Body::empty()).unwrap();
    let (s, b, _) = send(router, req).await;
    if b.is_empty() {
        return (s, Value::Null);
    }
    (s, serde_json::from_slice(&b).unwrap())
}

async fn record_passing_proofs(router: &Router, id: &str, digest: &str) {
    for (kind, detail) in [
        ("unit-test", "128 passed"),
        ("integration-test", "42 passed"),
        ("security-scan", "0 vulnerabilities"),
    ] {
        let (s, _, _) = send(
            router,
            post_json(
                &format!("/artifacts/{id}/proofs"),
                json!({"kind": kind, "digest": digest, "detail": detail, "passed": true}),
            ),
        )
        .await;
        assert_eq!(
            s,
            StatusCode::CREATED,
            "recording proof {kind} should succeed"
        );
    }
}

#[tokio::test]
async fn happy_path_development_to_release() {
    let r = router();
    let content = b"build-output-v1";

    // 注册：服务端计算并绑定摘要
    let (s, v) = register(&r, content).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(v["stage"], "development");
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();
    assert_eq!(digest, digest_of(content));
    assert_eq!(v["build_count"], 1);

    // 列表 / 单查
    let (s, list) = get_json(&r, "/artifacts").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(list.as_array().unwrap().len(), 1);
    let (s, fetched) = get_json(&r, &format!("/artifacts/{id}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(fetched["digest"], digest);

    // 三条必要证明（引用同一摘要）
    record_passing_proofs(&r, &id, &digest).await;

    // development -> validation（无需审批）
    let (s, v, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": false}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let v: Value = serde_json::from_slice(&v).unwrap();
    assert_eq!(v["stage"], "validation");

    // validation -> release：审批 + 证明齐全
    let (s, v, headers) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "alice"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert!(headers.contains_key("location"));
    let v: Value = serde_json::from_slice(&v).unwrap();
    assert_eq!(v["stage"], "release");

    // 重复晋级（终点）-> 409
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "alice"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);

    // 历史完整：1 注册 + 3 证明 + 2 晋级
    let (s, hist) = get_json(&r, &format!("/artifacts/{id}/history")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(hist["history"].as_array().unwrap().len(), 6);
}

#[tokio::test]
async fn release_gate_requires_approval_and_passing_proofs() {
    let r = router();
    let content = b"build-gate";
    let (_, v) = register(&r, content).await;
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();

    // 先到 validation
    let (s, _, _) = send(
        &r,
        post_json(&format!("/artifacts/{id}/promote"), json!({})),
    )
    .await;
    assert_eq!(s, StatusCode::OK);

    // 无审批 -> 422
    let (s, body, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": false}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    let body: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["error"], "precondition_failed");

    // 有审批但审批人为空 -> 400
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "  "}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // 有审批但零证明 -> 422，错误信息列出缺失证明
    let (s, body, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "alice"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    let body: Value = serde_json::from_slice(&body).unwrap();
    let msg = body["message"].as_str().unwrap();
    assert!(msg.contains("unit-test"));
    assert!(msg.contains("integration-test"));
    assert!(msg.contains("security-scan"));

    // 一条失败的证明不能满足门禁
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/proofs"),
            json!({"kind": "unit-test", "digest": digest, "passed": false, "detail": "2 failures"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
    let (s, body, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "alice"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    let body: Value = serde_json::from_slice(&body).unwrap();
    assert!(body["message"].as_str().unwrap().contains("unit-test"));

    // 补齐通过的证明后发布成功
    record_passing_proofs(&r, &id, &digest).await;
    let (s, v, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "alice"}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let v: Value = serde_json::from_slice(&v).unwrap();
    assert_eq!(v["stage"], "release");
}

#[tokio::test]
async fn content_is_immutable_after_registration() {
    let r = router();
    let original = b"immutable-content";
    let (_, v) = register(&r, original).await;
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();

    // 审批后（release）再尝试替换内容：仍然 409
    record_passing_proofs(&r, &id, &digest).await;
    let _ = send(
        &r,
        post_json(&format!("/artifacts/{id}/promote"), json!({})),
    )
    .await;
    let _ = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/promote"),
            json!({"approved": true, "approver": "bob"}),
        ),
    )
    .await;

    let (s, body, _) = send(
        &r,
        Request::builder()
            .method("POST")
            .uri(format!("/artifacts/{id}/content/immutable"))
            .header("content-type", "application/octet-stream")
            .body(Body::from("totally-different-bytes".to_string()))
            .unwrap(),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    let body: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["error"], "conflict");

    // 未知制品 -> 404
    let (s, _, _) = send(
        &r,
        Request::builder()
            .method("POST")
            .uri("/artifacts/does-not-exist/content/immutable")
            .body(Body::from("x".to_string()))
            .unwrap(),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    // 内容字节与注册时逐字节一致，响应头回带摘要
    let req = Request::builder()
        .uri(format!("/artifacts/{id}/content"))
        .body(Body::empty())
        .unwrap();
    let (s, bytes, headers) = send(&r, req).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(bytes, original);
    assert_eq!(
        headers.get("x-content-digest").unwrap().to_str().unwrap(),
        digest
    );
}

#[tokio::test]
async fn proof_must_reference_bound_digest() {
    let r = router();
    let (_, v) = register(&r, b"digest-binding").await;
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();

    // 格式非法 -> 400
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/proofs"),
            json!({"kind": "unit-test", "digest": "not-a-digest", "passed": true}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // 格式合法但指向别的内容 -> 409（摘要绑定校验）
    let other = digest_of(b"something-else");
    assert_ne!(other, digest);
    let (s, body, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/proofs"),
            json!({"kind": "unit-test", "digest": other, "passed": true}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    let body: Value = serde_json::from_slice(&body).unwrap();
    assert!(body["message"].as_str().unwrap().contains("does not match"));

    // 大写 / 不带前缀的等价写法应被规范化接受
    let upper = digest.to_uppercase();
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/proofs"),
            json!({"kind": "unit-test", "digest": upper, "passed": true}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);

    // 未知制品登记证明 -> 404
    let (s, _, _) = send(
        &r,
        post_json(
            "/artifacts/nope/proofs",
            json!({"kind": "unit-test", "digest": digest, "passed": true}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn concurrent_duplicate_promotion_only_one_succeeds() {
    let r = router();
    let (_, v) = register(&r, b"concurrent-promote").await;
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();
    record_passing_proofs(&r, &id, &digest).await;
    let _ = send(
        &r,
        post_json(&format!("/artifacts/{id}/promote"), json!({})),
    )
    .await;

    // 6 个并发晋级请求：恰好 1 个 200，其余 409
    let mut set = tokio::task::JoinSet::new();
    for _ in 0..6 {
        let r = r.clone();
        let id = id.clone();
        set.spawn(async move {
            let (s, _, _) = send(
                &r,
                post_json(
                    &format!("/artifacts/{id}/promote"),
                    json!({"approved": true, "approver": "alice"}),
                ),
            )
            .await;
            s
        });
    }
    let (mut ok, mut conflict) = (0u32, 0u32);
    while let Some(res) = set.join_next().await {
        match res.unwrap() {
            StatusCode::OK => ok += 1,
            StatusCode::CONFLICT => conflict += 1,
            other => panic!("unexpected status {other}"),
        }
    }
    assert_eq!(ok, 1, "exactly one promotion must win");
    assert_eq!(conflict, 5);

    let (s, cur) = get_json(&r, &format!("/artifacts/{id}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(cur["stage"], "release");

    // 历史中 validation->release 的 Promoted 事件只有一条
    let (_, hist) = get_json(&r, &format!("/artifacts/{id}/history")).await;
    let promotions = hist["history"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["type"] == "promoted" && e["from"] == "validation" && e["to"] == "release")
        .count();
    assert_eq!(promotions, 1);
}

#[tokio::test]
async fn concurrent_rollback_only_one_succeeds_and_history_is_kept() {
    let r = router();
    let content = b"rollback-content";
    let (_, v) = register(&r, content).await;
    let id = v["id"].as_str().unwrap().to_string();
    let digest = v["digest"].as_str().unwrap().to_string();
    record_passing_proofs(&r, &id, &digest).await;
    // 到达 validation：只剩 validation->development 一级可回退，
    // 从而能确定性地断言"同一跃迁并发时恰好 1 个成功、其余 409"。
    let _ = send(
        &r,
        post_json(&format!("/artifacts/{id}/promote"), json!({})),
    )
    .await;

    // 并发回退 validation->development：恰好 1 个成功。
    let mut set = tokio::task::JoinSet::new();
    for i in 0..5 {
        let r = r.clone();
        let id = id.clone();
        set.spawn(async move {
            send(
                &r,
                post_json(
                    &format!("/artifacts/{id}/rollback"),
                    json!({"reason": format!("concurrent-{i}")}),
                ),
            )
            .await
            .0
        });
    }
    let (mut ok, mut conflict) = (0u32, 0u32);
    while let Some(res) = set.join_next().await {
        match res.unwrap() {
            StatusCode::OK => ok += 1,
            StatusCode::CONFLICT => conflict += 1,
            other => panic!("unexpected status {other}"),
        }
    }
    assert_eq!(ok, 1);
    assert_eq!(conflict, 4);

    // 当前阶段回到 development；摘要不变、内容不变、build_count 不增加（未重建）
    let (s, cur) = get_json(&r, &format!("/artifacts/{id}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(cur["stage"], "development");
    assert_eq!(cur["digest"], digest);
    assert_eq!(cur["build_count"], 1);

    let (_, bytes, _) = send(
        &r,
        Request::builder()
            .uri(format!("/artifacts/{id}/content"))
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(bytes, content);

    // 历史保留全部事件（注册+3证明+1晋级+1回退 = 6），回退不删历史
    let (_, hist) = get_json(&r, &format!("/artifacts/{id}/history")).await;
    let events = hist["history"].as_array().unwrap();
    assert_eq!(events.len(), 6);
    let types: HashSet<&str> = events.iter().map(|e| e["type"].as_str().unwrap()).collect();
    for t in ["registered", "proof-recorded", "promoted", "rolled-back"] {
        assert!(types.contains(t), "history missing event type {t}");
    }

    // 已在 development，再回退 -> 409
    let (s, _, _) = send(
        &r,
        post_json(&format!("/artifacts/{id}/rollback"), json!({})),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);

    // 回退后仍可凭同一摘要、同一批证明重新晋级（无需重新构建内容）
    let (s, v, _) = send(
        &r,
        post_json(&format!("/artifacts/{id}/promote"), json!({})),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let v: Value = serde_json::from_slice(&v).unwrap();
    assert_eq!(v["stage"], "validation");
    assert_eq!(v["digest"], digest);
}

#[tokio::test]
async fn content_addressed_storage_is_not_rebuilt() {
    let r = router();
    let content = b"same-build-output";

    let (_, a) = register(&r, content).await;
    let digest = a["digest"].as_str().unwrap().to_string();
    let (_, b) = register(&r, content).await;

    // 两个制品绑定同一摘要
    assert_eq!(a["digest"], b["digest"]);

    // blob 层：引用计数 2（内容只存一份）
    let (s, info) = get_json(&r, &format!("/blobs/{digest}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(info["stored"], true);
    assert_eq!(info["reference_count"], 2);

    // 第二个制品 build_count=1，且两者内容相同
    assert_eq!(b["build_count"], 1);
    let (_, bytes_a, _) = send(
        &r,
        Request::builder()
            .uri(format!("/artifacts/{}/content", a["id"].as_str().unwrap()))
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    let (_, bytes_b, _) = send(
        &r,
        Request::builder()
            .uri(format!("/artifacts/{}/content", b["id"].as_str().unwrap()))
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(bytes_a, bytes_b);

    // 未知摘要 -> stored=false（200 响应体表达，便于脚本探测）
    let ghost = digest_of(b"ghost");
    let (s, info) = get_json(&r, &format!("/blobs/{ghost}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(info["stored"], false);
}

#[tokio::test]
async fn invalid_json_and_bad_paths_are_handled() {
    let r = router();
    let (_, v) = register(&r, b"err-paths").await;
    let id = v["id"].as_str().unwrap().to_string();

    // 非法 JSON -> 400
    let (s, _, _) = send(
        &r,
        Request::builder()
            .method("POST")
            .uri(format!("/artifacts/{id}/promote"))
            .header("content-type", "application/json")
            .body(Body::from("{not json"))
            .unwrap(),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // 缺少必需字段 kind -> 422（Axum 0.8 的 Json 反序列化失败为 422）
    let (s, _, _) = send(
        &r,
        post_json(
            &format!("/artifacts/{id}/proofs"),
            json!({"digest": v["digest"], "passed": true}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);

    // 查询不存在的制品 -> 404
    let (s, _) = get_json(&r, "/artifacts/00000000-0000-0000-0000-000000000000").await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    // 健康检查
    let (s, h) = get_json(&r, "/health").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(h["status"], "ok");
}
