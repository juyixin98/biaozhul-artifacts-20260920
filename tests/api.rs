//! 集成测试：真实启动 HTTP 服务，覆盖验收场景。
//! - 上传/下载回环与摘要校验
//! - 缺块发布被拒绝，且不产生 AC 记录
//! - 缓存对象损坏后，下载与命中均不得返回伪成功
//! - 并发上传相同动作/相同块：幂等、无伪成功
//! - 缓存命中与未命中（重新执行）路径

use build_cache::{build_router, sha256_hex, AppState, Manifest, OutputEntry};
use reqwest::StatusCode;

struct TestServer {
    base: String,
    data_dir: tempfile::TempDir,
    _handle: tokio::task::JoinHandle<()>,
}

async fn start_server() -> TestServer {
    let data_dir = tempfile::tempdir().unwrap();
    let state = AppState::new(data_dir.path()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = tokio::spawn(async move {
        axum::serve(listener, build_router(state)).await.unwrap();
    });
    TestServer {
        base: format!("http://{addr}"),
        data_dir,
        _handle: handle,
    }
}

fn manifest_for(outputs: &[(&str, &[u8])]) -> Manifest {
    Manifest {
        exit_code: 0,
        outputs: outputs
            .iter()
            .map(|(path, data)| OutputEntry {
                path: path.to_string(),
                digest: sha256_hex(data),
                size: data.len() as u64,
            })
            .collect(),
    }
}

#[tokio::test]
async fn upload_download_roundtrip() {
    let srv = start_server().await;
    let client = reqwest::Client::new();
    let content = b"hello build cache";
    let digest = sha256_hex(content);

    let resp = client
        .put(format!("{}/cas/{digest}", srv.base))
        .body(content.to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    // 重复上传同一对象：幂等 200。
    let resp = client
        .put(format!("{}/cas/{digest}", srv.base))
        .body(content.to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let resp = client
        .get(format!("{}/cas/{digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.bytes().await.unwrap().as_ref(), content);
}

#[tokio::test]
async fn upload_with_wrong_digest_rejected() {
    let srv = start_server().await;
    let client = reqwest::Client::new();
    let wrong = sha256_hex(b"other");
    let resp = client
        .put(format!("{}/cas/{wrong}", srv.base))
        .body(b"actual content".to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    // 服务端不得留存该对象。
    let resp = client
        .get(format!("{}/cas/{wrong}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn publish_with_missing_blob_rejected_and_not_stored() {
    let srv = start_server().await;
    let client = reqwest::Client::new();

    let present = b"output-a";
    let present_digest = sha256_hex(present);
    client
        .put(format!("{}/cas/{present_digest}", srv.base))
        .body(present.to_vec())
        .send()
        .await
        .unwrap();

    // 清单引用一个未上传的块。
    let missing_digest = sha256_hex(b"never-uploaded");
    let action_digest = sha256_hex(b"action:gcc -c missing.c");
    let manifest = Manifest {
        exit_code: 0,
        outputs: vec![
            OutputEntry {
                path: "a.o".into(),
                digest: present_digest.clone(),
                size: present.len() as u64,
            },
            OutputEntry {
                path: "b.o".into(),
                digest: missing_digest,
                size: 14,
            },
        ],
    };
    let resp = client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&manifest)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["missing"].as_array().unwrap().len(), 1);

    // 关键：发布失败后 AC 中不得有记录（不得伪成功）。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn publish_then_cache_hit_and_miss() {
    let srv = start_server().await;
    let client = reqwest::Client::new();

    let out = b"compiled object bytes";
    let out_digest = sha256_hex(out);
    client
        .put(format!("{}/cas/{out_digest}", srv.base))
        .body(out.to_vec())
        .send()
        .await
        .unwrap();

    let action_digest = sha256_hex(b"action:gcc -c main.c");
    // 未命中 -> 客户端应重新执行。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // “执行”完成后发布。
    let manifest = manifest_for(&[("main.o", out)]);
    let resp = client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&manifest)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    // 命中。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let got: Manifest = resp.json().await.unwrap();
    assert_eq!(got, manifest);

    // 命中后按清单下载输出并校验摘要。
    let resp = client
        .get(format!("{}/cas/{}", srv.base, got.outputs[0].digest))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = resp.bytes().await.unwrap();
    assert_eq!(sha256_hex(&bytes), got.outputs[0].digest);
}

#[tokio::test]
async fn corrupt_cas_object_fails_download_and_action_hit() {
    let srv = start_server().await;
    let client = reqwest::Client::new();

    let out = b"integrity matters";
    let out_digest = sha256_hex(out);
    client
        .put(format!("{}/cas/{out_digest}", srv.base))
        .body(out.to_vec())
        .send()
        .await
        .unwrap();
    let action_digest = sha256_hex(b"action:corruption-test");
    let manifest = manifest_for(&[("x.o", out)]);
    client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&manifest)
        .send()
        .await
        .unwrap();

    // 直接篡改磁盘上的 CAS 对象，模拟存储损坏。
    let cas_file = srv
        .data_dir
        .path()
        .join("cas")
        .join(&out_digest[..2])
        .join(&out_digest);
    std::fs::write(&cas_file, b"tampered bytes").unwrap();

    // 下载必须报错，不得返回损坏内容。
    let resp = client
        .get(format!("{}/cas/{out_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);

    // 动作命中也必须报错，不得伪成功。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let body: serde_json::Value = resp.json().await.unwrap();
    assert_eq!(body["corrupt"].as_array().unwrap().len(), 1);

    // 损坏后重新发布同一动作同样被拒绝。
    let resp = client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&manifest)
        .send()
        .await
        .unwrap();
    assert!(resp.status().is_client_error() || resp.status().is_server_error());
    assert_ne!(resp.status(), StatusCode::OK);
    assert_ne!(resp.status(), StatusCode::CREATED);
}

#[tokio::test]
async fn concurrent_uploads_same_blob_and_action() {
    let srv = start_server().await;
    let client = reqwest::Client::new();

    let content = b"shared output from parallel build";
    let digest = sha256_hex(content);

    // 16 个并发上传同一块：全部成功。
    let mut uploads = Vec::new();
    for _ in 0..16 {
        let c = client.clone();
        let url = format!("{}/cas/{digest}", srv.base);
        uploads.push(tokio::spawn(async move {
            c.put(url).body(content.to_vec()).send().await.unwrap().status()
        }));
    }
    for u in uploads {
        let status = u.await.unwrap();
        assert!(status == StatusCode::OK || status == StatusCode::CREATED);
    }

    // 16 个并发发布同一动作（内容一致）：全部成功。
    let action_digest = sha256_hex(b"action:parallel-compile");
    let manifest = manifest_for(&[("shared.o", content)]);
    let mut publishes = Vec::new();
    for _ in 0..16 {
        let c = client.clone();
        let url = format!("{}/ac/{action_digest}", srv.base);
        let m = manifest.clone();
        publishes.push(tokio::spawn(async move {
            c.put(url).json(&m).send().await.unwrap().status()
        }));
    }
    for p in publishes {
        let status = p.await.unwrap();
        assert!(status == StatusCode::OK || status == StatusCode::CREATED);
    }

    // 最终命中且内容正确。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let got: Manifest = resp.json().await.unwrap();
    assert_eq!(got, manifest);
}

#[tokio::test]
async fn conflicting_publish_same_action_rejected() {
    let srv = start_server().await;
    let client = reqwest::Client::new();

    let out_a = b"result A";
    let out_b = b"result B";
    for data in [out_a.as_slice(), out_b.as_slice()] {
        let d = sha256_hex(data);
        client
            .put(format!("{}/cas/{d}", srv.base))
            .body(data.to_vec())
            .send()
            .await
            .unwrap();
    }
    let action_digest = sha256_hex(b"action:conflict");
    let m1 = manifest_for(&[("out", out_a)]);
    let m2 = manifest_for(&[("out", out_b)]);

    let resp = client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&m1)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);

    // 同一动作用不同清单重复发布：不可变，必须冲突。
    let resp = client
        .put(format!("{}/ac/{action_digest}", srv.base))
        .json(&m2)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);

    // 缓存内容仍是第一次发布的。
    let resp = client
        .get(format!("{}/ac/{action_digest}", srv.base))
        .send()
        .await
        .unwrap();
    let got: Manifest = resp.json().await.unwrap();
    assert_eq!(got, m1);
}
