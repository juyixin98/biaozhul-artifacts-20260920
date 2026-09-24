//! HTTP-level integration tests: exercise the Axum router in-process.

use std::fs;
use std::path::Path;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use tempfile::TempDir;
use tower::ServiceExt;

use repro_pack::server::app;

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

async fn call(uri: &str, body: Value) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: Value = serde_json::from_slice(&bytes).unwrap_or_else(|e| {
        panic!(
            "non-JSON response: {e}: {}",
            String::from_utf8_lossy(&bytes)
        )
    });
    (status, json)
}

#[tokio::test]
async fn health_ok() {
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let v: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["status"], "ok");
}

#[tokio::test]
async fn pack_then_verify_over_http() {
    let td = TempDir::new().unwrap();
    let src = td.path().join("src");
    let out = td.path().join("out");
    fs::create_dir_all(src.join("dir")).unwrap();
    write_file(&src.join("a.txt"), b"hello\n");
    write_file(&src.join("dir/b"), b"exec\n");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(src.join("dir/b"), fs::Permissions::from_mode(0o755)).unwrap();
    }

    let (st, v) = call(
        "/pack",
        json!({
            "source": src.to_string_lossy(),
            "output_dir": out.to_string_lossy(),
            "artifact_name": "svc"
        }),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["entry_count"], 4);
    assert!(v["sha256"].is_string());
    assert!(out.join("svc.tar").exists());
    assert!(out.join("svc.manifest.json").exists());
    assert!(out.join("svc.sha256").exists());

    let (st, v) = call(
        "/verify",
        json!({
            "tar_path": out.join("svc.tar").to_string_lossy(),
            "source": src.to_string_lossy()
        }),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["ok"], true);
    assert_eq!(v["digest_matches"], true);
    assert_eq!(v["rebuild_matches"], true);
}

#[tokio::test]
async fn pack_rejects_escaping_symlink_with_409() {
    let td = TempDir::new().unwrap();
    let src = td.path().join("src");
    let out = td.path().join("out");
    fs::create_dir_all(&src).unwrap();
    write_file(&src.join("f"), b"x");
    std::os::unix::fs::symlink("../../etc/passwd", src.join("evil")).unwrap();

    let (st, v) = call(
        "/pack",
        json!({
            "source": src.to_string_lossy(),
            "output_dir": out.to_string_lossy()
        }),
    )
    .await;
    assert_eq!(st, StatusCode::CONFLICT, "body: {v}");
    assert_eq!(v["error"]["code"], "canonical_path_conflict");
    assert!(v["error"]["message"].as_str().unwrap().contains("escapes"));
}

#[tokio::test]
async fn pack_rejects_missing_source_with_400() {
    let td = TempDir::new().unwrap();
    let (st, v) = call(
        "/pack",
        json!({
            "source": td.path().join("nope").to_string_lossy(),
            "output_dir": td.path().join("out").to_string_lossy()
        }),
    )
    .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["code"], "source_not_found");
}

#[tokio::test]
async fn malformed_json_is_400() {
    let req = Request::builder()
        .method("POST")
        .uri("/pack")
        .header("content-type", "application/json")
        .body(Body::from("{ not json"))
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn verify_returns_422_on_content_mismatch() {
    let td = TempDir::new().unwrap();
    let src = td.path().join("src");
    let out = td.path().join("out");
    fs::create_dir_all(&src).unwrap();
    write_file(&src.join("f"), b"original");
    let (_, packv) = call(
        "/pack",
        json!({
            "source": src.to_string_lossy(),
            "output_dir": out.to_string_lossy(),
            "artifact_name": "x"
        }),
    )
    .await;
    assert!(packv["sha256"].is_string());

    write_file(&src.join("f"), b"changed!!");
    let (st, v) = call(
        "/verify",
        json!({
            "tar_path": out.join("x.tar").to_string_lossy(),
            "source": src.to_string_lossy()
        }),
    )
    .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["ok"], false);
    assert_eq!(v["rebuild_matches"], false);
}

#[tokio::test]
async fn determinism_two_locations_same_content_match() {
    let td = TempDir::new().unwrap();
    let a_src = td.path().join("loc-a/src");
    let b_src = td.path().join("loc-b/src");
    fs::create_dir_all(&a_src).unwrap();
    fs::create_dir_all(&b_src).unwrap();
    for root in [&a_src, &b_src] {
        write_file(&root.join("z/last"), b"z");
        write_file(&root.join("a/first"), b"a");
        std::os::unix::fs::symlink("a/first", root.join("lnk")).unwrap();
    }
    let (_, va) = call(
        "/pack",
        json!({"source": a_src.to_string_lossy(), "output_dir": td.path().join("oa"), "artifact_name": "p"}),
    )
    .await;
    let (_, vb) = call(
        "/pack",
        json!({"source": b_src.to_string_lossy(), "output_dir": td.path().join("ob"), "artifact_name": "p"}),
    )
    .await;
    assert_eq!(va["sha256"], vb["sha256"]);
    assert_eq!(
        fs::read(td.path().join("oa/p.tar")).unwrap(),
        fs::read(td.path().join("ob/p.tar")).unwrap()
    );
}
