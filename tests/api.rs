//! 端到端集成测试：通过真正的 HTTP 栈（tower::ServiceExt::oneshot）验证
//! 全部验收场景。

use std::collections::BTreeMap;

use remote_build_cache::models::{
    Action, ActionResult, Digest, FindMissingRequest, OutputFile, OutputDirectory,
    PublishActionRequest,
};
use remote_build_cache::{app, DEFAULT_MAX_BLOB_BYTES};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;

type Router = axum::Router;

async fn test_app() -> (Router, TempDir) {
    let dir = TempDir::new().unwrap();
    let app = app(dir.path(), DEFAULT_MAX_BLOB_BYTES).await;
    (app, dir)
}

fn sha256(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(data))
}

fn digest_of(data: &[u8]) -> Digest {
    Digest {
        hash: sha256(data),
        size_bytes: data.len() as u64,
    }
}

async fn send(
    app: &Router,
    method: &str,
    uri: &str,
    body: Option<Vec<u8>>,
    content_type: Option<&str>,
) -> (StatusCode, Vec<u8>, Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(ct) = content_type {
        builder = builder.header("content-type", ct);
    }
    let req = builder.body(Body::from(body.unwrap_or_default())).unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes()
        .to_vec();
    let json: Value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, bytes, json)
}

async fn put_blob(app: &Router, data: &[u8]) -> Digest {
    let d = digest_of(data);
    let uri = format!("/blobs/{}?size_bytes={}", d.hash, d.size_bytes);
    let (status, _, body) = send(app, "PUT", &uri, Some(data.to_vec()), None).await;
    assert_eq!(status, StatusCode::OK, "put blob body: {body}");
    d
}

fn example_action() -> Action {
    let mut platform = BTreeMap::new();
    platform.insert("os".to_string(), "linux".to_string());
    platform.insert("cpu".to_string(), "x86_64".to_string());
    Action {
        arguments: vec!["gcc".into(), "-c".into(), "main.c".into(), "-o".into(), "main.o".into()],
        input_root_digest: None,
        platform,
        environment_variables: BTreeMap::new(),
        working_directory: ".".into(),
    }
}

async fn action_digest_via_api(app: &Router, action: &Action) -> (String, u64) {
    let (status, _, body) = send(
        app,
        "POST",
        "/util/action-digest",
        Some(serde_json::to_vec(action).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    (
        body["hash"].as_str().unwrap().to_string(),
        body["size_bytes"].as_u64().unwrap(),
    )
}

// ---------------- 场景 1：缓存命中 vs 未命中（重新执行路径） ----------------

#[tokio::test]
async fn miss_then_publish_then_hit() {
    let (app, _dir) = test_app().await;

    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;
    let action_uri = format!("/actions/{action_hash}");

    // 1) 发布前查询 → 404 cache_miss（客户端应重新执行）
    let (status, _, body) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"], "cache_miss");

    // 2) 准备“重新执行”后的输出对象
    let obj = put_blob(&app, b"fake-object-file-content").await;
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: Some("compiled\n".into()),
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "main.o".into(),
            digest: obj.clone(),
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };

    // 3) 缺块发布（对象还没“上传”的情况见下一个测试）；这里对象齐全 → 发布成功
    let req = PublishActionRequest {
        action: action.clone(),
        action_result: result.clone(),
    };
    let (status, _, body) = send(
        &app,
        "PUT",
        &action_uri,
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "publish: {body}");
    assert_eq!(body["status"], "published");
    assert!(body["action_result"]["published_at_ms"].is_number());

    // 4) 再次查询 → 200 HIT
    let (status, _, body) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "HIT");
    assert_eq!(
        body["action_result"]["output_files"][0]["digest"]["hash"],
        obj.hash
    );
}

// ---------------- 场景 2：缺块发布必须失败，且不得产生动作记录 ----------------

#[tokio::test]
async fn publish_with_missing_blob_is_rejected() {
    let (app, _dir) = test_app().await;
    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;
    let action_uri = format!("/actions/{action_hash}");

    // 声明一个从未上传的输出块
    let ghost = digest_of(b"never uploaded bytes");
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "out.bin".into(),
            digest: ghost,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let req = PublishActionRequest {
        action,
        action_result: result,
    };

    let (status, _, body) = send(
        &app,
        "PUT",
        &action_uri,
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    // 424 Failed Dependency：绝不返回伪成功
    assert_eq!(status, StatusCode::FAILED_DEPENDENCY, "{body}");
    assert_eq!(body["error"], "missing_blobs");
    assert!(body["message"].as_str().unwrap().contains("missing from CAS"));

    // 动作记录必须不存在：后续 GET 仍是 404 cache_miss，而不是损坏或命中
    let (status, _, body) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"], "cache_miss");
}

// ---------------- 场景 3：递归目录 + stdout/stderr 块缺失也拦截 ----------------

#[tokio::test]
async fn missing_blob_in_nested_directory_and_stdout_is_rejected() {
    let (app, _dir) = test_app().await;
    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;

    let present = put_blob(&app, b"present-file").await;
    let missing = digest_of(b"missing-deep-file");
    let missing_log = digest_of(b"missing stdout blob");

    let result = ActionResult {
        exit_code: 0,
        stdout_digest: Some(missing_log),
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![],
        output_directories: vec![OutputDirectory {
            path: "out".into(),
            files: vec![OutputFile {
                path: "out/a".into(),
                digest: present,
                is_executable: false,
            }],
            directories: vec![OutputDirectory {
                path: "out/sub".into(),
                files: vec![OutputFile {
                    path: "out/sub/b".into(),
                    digest: missing,
                    is_executable: false,
                }],
                directories: vec![],
            }],
        }],
        published_at_ms: None,
    };
    let req = PublishActionRequest {
        action,
        action_result: result,
    };
    let uri = format!("/actions/{action_hash}");
    let (status, _, body) = send(
        &app,
        "PUT",
        &uri,
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::FAILED_DEPENDENCY, "{body}");
    // 两个缺失对象都应在消息中列出（去重）
    let msg = body["message"].as_str().unwrap();
    assert!(msg.contains("2 referenced blob(s) missing"), "{msg}");
}

// ---------------- 场景 4：缓存对象损坏时不得返回伪成功 ----------------

#[tokio::test]
async fn corrupted_blob_download_is_rejected() {
    let (app, dir) = test_app().await;
    let data = b"important build artifact";
    let d = put_blob(&app, data).await;

    // 正常下载
    let uri = format!("/blobs/{}", d.hash);
    let (status, bytes, _) = send(&app, "GET", &uri, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes, data);

    // 直接在磁盘上篡改内容（模拟位腐烂/外部破坏）
    let path = dir
        .path()
        .join("cas")
        .join(&d.hash[0..2])
        .join(&d.hash[2..4])
        .join(&d.hash);
    tokio::fs::write(&path, b"tampered content").await.unwrap();

    // GET 必须 503，且返回的绝不是坏数据
    let (status, bytes, body) = send(&app, "GET", &uri, None, None).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"], "cache_corruption");
    assert_ne!(bytes, b"tampered content");

    // HEAD 视为不存在
    let (status, _, _) = send(&app, "HEAD", &uri, None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    // find-missing 把损坏对象报为缺失
    let (status, _, body) = send(
        &app,
        "POST",
        "/find-missing",
        Some(
            serde_json::to_vec(&FindMissingRequest {
                digests: vec![d.clone()],
            })
            .unwrap(),
        ),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["missing"][0]["hash"], json!(d.hash));

    // 重新上传正确内容 → 原子替换坏文件并自愈，随后下载恢复正常
    let (status, _, _) = send(
        &app,
        "PUT",
        &format!("/blobs/{}?size_bytes={}", d.hash, d.size_bytes),
        Some(data.to_vec()),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (status, bytes, _) = send(&app, "GET", &uri, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes, data);
}

#[tokio::test]
async fn corrupted_referenced_blob_breaks_action_hit() {
    let (app, dir) = test_app().await;
    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;
    let action_uri = format!("/actions/{action_hash}");

    let data = b"artifact pre-corruption";
    let d = put_blob(&app, data).await;
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "f".into(),
            digest: d.clone(),
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let req = PublishActionRequest {
        action: action.clone(),
        action_result: result,
    };
    let (status, _, _) = send(
        &app,
        "PUT",
        &action_uri,
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);

    // 命中正常
    let (status, _, _) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::OK);

    // 篡改引用的 CAS 对象
    let path = dir
        .path()
        .join("cas")
        .join(&d.hash[0..2])
        .join(&d.hash[2..4])
        .join(&d.hash);
    tokio::fs::write(&path, b"x").await.unwrap();

    // 命中路径必须 503，而不是 200 伪成功；也不是 404（记录还在，只是不可信）
    let (status, _, body) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["error"], "cache_corruption");

    // 删除引用对象（悬空引用）同样 503
    tokio::fs::remove_file(&path).await.unwrap();
    let (status, _, body) = send(&app, "GET", &action_uri, None, None).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
}

// ---------------- 场景 5：并发上传相同动作 / 相同块 ----------------

#[tokio::test]
async fn concurrent_identical_blob_uploads_succeed_once_consistent() {
    use std::sync::Arc;
    let (app, _dir) = test_app().await;
    let data: Vec<u8> = (0u8..=255).cycle().take(100_000).collect();
    let d = digest_of(&data);

    let app = Arc::new(app);
    let mut handles = Vec::new();
    for _ in 0..16 {
        let app = app.clone();
        let data = data.clone();
        let d = d.clone();
        handles.push(tokio::spawn(async move {
            let uri = format!("/blobs/{}?size_bytes={}", d.hash, d.size_bytes);
            let req = Request::builder()
                .method("PUT")
                .uri(uri)
                .body(Body::from(data))
                .unwrap();
            let resp = (*app).clone().oneshot(req).await.unwrap();
            resp.status()
        }));
    }
    for h in handles {
        assert_eq!(h.await.unwrap(), StatusCode::OK);
    }

    // 落盘内容必须正确且只有一个对象
    let (status, bytes, _) =
        send(&app, "GET", &format!("/blobs/{}", d.hash), None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes.len(), 100_000);
    assert_eq!(sha256(&bytes), d.hash);
}

#[tokio::test]
async fn concurrent_identical_action_publishes_all_agree() {
    use std::sync::Arc;
    let (app, _dir) = test_app().await;

    let obj = {
        // 先上传输出对象（并发发布时大家引用同一个块）
        let d = digest_of(b"shared output");
        let uri = format!("/blobs/{}?size_bytes={}", d.hash, d.size_bytes);
        let (s, _, _) = send(&app, "PUT", &uri, Some(b"shared output".to_vec()), None).await;
        assert_eq!(s, StatusCode::OK);
        d
    };

    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: Some("ok".into()),
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "main.o".into(),
            digest: obj,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let payload = serde_json::to_vec(&PublishActionRequest {
        action,
        action_result: result,
    })
    .unwrap();

    let app = Arc::new(app);
    let uri = format!("/actions/{action_hash}");
    let mut handles = Vec::new();
    for _ in 0..12 {
        let app = app.clone();
        let uri = uri.clone();
        let payload = payload.clone();
        handles.push(tokio::spawn(async move {
            let req = Request::builder()
                .method("PUT")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(payload))
                .unwrap();
            let resp = (*app).clone().oneshot(req).await.unwrap();
            resp.status()
        }));
    }
    let mut statuses = Vec::new();
    for h in handles {
        statuses.push(h.await.unwrap());
    }
    // 相同内容的幂等并发发布：全部成功（首个写入，其余幂等确认）
    assert!(
        statuses.iter().all(|s| *s == StatusCode::OK),
        "statuses: {statuses:?}"
    );

    let (status, _, body) = send(&app, "GET", &uri, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "HIT");
}

#[tokio::test]
async fn concurrent_publish_while_blob_missing_all_fail_and_nothing_published() {
    use std::sync::Arc;
    let (app, _dir) = test_app().await;
    let action = example_action();
    let (action_hash, _) = action_digest_via_api(&app, &action).await;

    let ghost = digest_of(b"ghost object never uploaded");
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "ghost".into(),
            digest: ghost,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let payload = serde_json::to_vec(&PublishActionRequest {
        action,
        action_result: result,
    })
    .unwrap();

    let app = Arc::new(app);
    let uri = format!("/actions/{action_hash}");
    let mut handles = Vec::new();
    for _ in 0..12 {
        let app = app.clone();
        let uri = uri.clone();
        let payload = payload.clone();
        handles.push(tokio::spawn(async move {
            let req = Request::builder()
                .method("PUT")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(payload))
                .unwrap();
            (*app).clone().oneshot(req).await.unwrap().status()
        }));
    }
    let mut statuses = Vec::new();
    for h in handles {
        statuses.push(h.await.unwrap());
    }
    assert!(
        statuses
            .iter()
            .all(|s| *s == StatusCode::FAILED_DEPENDENCY),
        "expected all 424, got {statuses:?}"
    );

    // 没有任何一个并发请求偷偷写出动作记录
    let (status, _, body) = send(&app, "GET", &uri, None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
}

// ---------------- 场景 6：上传内容与摘要不符必须拒绝 ----------------

#[tokio::test]
async fn blob_upload_with_wrong_hash_is_rejected_and_not_stored() {
    let (app, _dir) = test_app().await;
    let data = b"actual body";
    let wrong = digest_of(b"different");
    let uri = format!("/blobs/{}?size_bytes={}", wrong.hash, data.len());
    let (status, _, body) = send(&app, "PUT", &uri, Some(data.to_vec()), None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "invalid_digest");

    // 以错误长度上传也拒绝
    let d = digest_of(data);
    let uri = format!("/blobs/{}?size_bytes=999", d.hash);
    let (status, _, _) = send(&app, "PUT", &uri, Some(data.to_vec()), None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    // 之后无法按错误键读到任何东西
    let (status, _, _) = send(&app, "HEAD", &format!("/blobs/{}", wrong.hash), None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

// ---------------- 场景 7：动作键与动作内容不符 ----------------

#[tokio::test]
async fn publish_rejects_action_key_mismatch() {
    let (app, _dir) = test_app().await;
    let action = example_action();
    let (real_hash, _) = action_digest_via_api(&app, &action).await;
    let fake_hash = sha256(b"something else");
    assert_ne!(real_hash, fake_hash);

    let obj = put_blob(&app, b"x").await;
    let result = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "f".into(),
            digest: obj,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let req = PublishActionRequest {
        action,
        action_result: result,
    };
    let (status, _, body) = send(
        &app,
        "PUT",
        &format!("/actions/{fake_hash}"),
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "action_key_mismatch");

    // 真实键下也不存在（校验失败先于一切写入）
    let (status, _, _) = send(&app, "GET", &format!("/actions/{real_hash}"), None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

// ---------------- 场景 8：不可变性 + 下载交叉校验 + 动作对象自动入 CAS ----------------

#[tokio::test]
async fn action_result_is_immutable_and_action_blob_stored() {
    let (app, _dir) = test_app().await;
    let action = example_action();
    let (hash, size) = action_digest_via_api(&app, &action).await;

    let obj = put_blob(&app, b"v1 content").await;
    let result_v1 = ActionResult {
        exit_code: 0,
        stdout_digest: None,
        stdout_raw: None,
        stderr_digest: None,
        stderr_raw: None,
        output_files: vec![OutputFile {
            path: "f".into(),
            digest: obj,
            is_executable: false,
        }],
        output_directories: vec![],
        published_at_ms: None,
    };
    let publish = |r: ActionResult| {
        let req = PublishActionRequest {
            action: action.clone(),
            action_result: r,
        };
        serde_json::to_vec(&req).unwrap()
    };

    let uri = format!("/actions/{hash}");
    let (status, _, body) = send(
        &app,
        "PUT",
        &uri,
        Some(publish(result_v1.clone())),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // 相同内容重复发布 → 幂等 200
    let (status, _, _) = send(
        &app,
        "PUT",
        &uri,
        Some(publish(result_v1.clone())),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);

    // 动作对象已自动存入 CAS，可用 GET 下载验证摘要
    let (status, bytes, _) = send(&app, "GET", &format!("/blobs/{hash}"), None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes.len() as u64, size);
    assert_eq!(sha256(&bytes), hash);

    // 不同结果发布到同一不可变键 → 409
    let obj2 = put_blob(&app, b"v2 content different").await;
    let mut result_v2 = result_v1.clone();
    result_v2.output_files[0].digest = obj2;
    let (status, _, body) = send(
        &app,
        "PUT",
        &uri,
        Some(publish(result_v2)),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"], "immutable_conflict");
}

#[tokio::test]
async fn download_with_expected_size_cross_check() {
    let (app, _dir) = test_app().await;
    let d = put_blob(&app, b"abcdef").await;

    let req = Request::builder()
        .method("GET")
        .uri(format!("/blobs/{}", d.hash))
        .header("x-expected-size-bytes", "999")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn find_missing_reports_only_absent_and_dedups() {
    let (app, _dir) = test_app().await;
    let present = put_blob(&app, b"here").await;
    let absent = digest_of(b"not here");

    let req = FindMissingRequest {
        digests: vec![present.clone(), absent.clone(), present.clone()],
    };
    let (status, _, body) = send(
        &app,
        "POST",
        "/find-missing",
        Some(serde_json::to_vec(&req).unwrap()),
        Some("application/json"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let missing = body["missing"].as_array().unwrap();
    assert_eq!(missing.len(), 1);
    assert_eq!(missing[0]["hash"], json!(absent.hash));
}
