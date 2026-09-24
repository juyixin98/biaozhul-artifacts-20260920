//! HTTP-level tests driven directly through the Axum router (no real port).
mod common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use bytes::Bytes;
use common::{build_image, tmp_workdir, workdir_for, Entry, Layer, RawEntry};
use http_body_util::BodyExt;
use oci_unpack::{Limits, Workdir};
use tower::ServiceExt;

fn app(tmp: &tempfile::TempDir) -> (Router, Arc<Workdir>) {
    let w = Arc::new(workdir_for(tmp.path()));
    let app = oci_unpack::server::router(w.clone(), Arc::new(Limits::default()));
    (app, w)
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, Bytes) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, bytes)
}

fn get(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}
fn post(path: &str) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn full_rebuild_api_flow() {
    let tmp = tmp_workdir();
    let (app, _w) = app(&tmp);
    build_image(
        &_w.images_dir(),
        "web",
        vec![
            Layer::new(vec![
                Entry::Dir("srv"),
                Entry::File("srv/a", b"one".to_vec(), 0o644),
            ]),
            Layer::new(vec![
                Entry::File("srv/.wh.a", vec![], 0o000),
                Entry::File("srv/b", b"two".to_vec(), 0o644),
            ]),
        ],
        None,
    );

    // health
    let (st, body) = send(&app, get("/health")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(body, "{\"status\":\"ok\"}");

    // images list
    let (st, body) = send(&app, get("/images")).await;
    assert_eq!(st, StatusCode::OK);
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["images"][0], "web");

    // before rebuild: 404
    let (st, _) = send(&app, get("/rebuild/web")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);

    // rebuild
    let (st, body) = send(&app, post("/rebuild/web")).await;
    assert_eq!(
        st,
        StatusCode::OK,
        "body: {}",
        String::from_utf8_lossy(&body)
    );
    let report: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(report["layerCount"], 2);
    assert!(report["rootfsDigest"]
        .as_str()
        .unwrap()
        .starts_with("sha256:"));
    let rootfs_digest = report["rootfsDigest"].as_str().unwrap().to_string();

    // deleted file absent, created file present in report
    let paths: Vec<&str> = report["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["path"].as_str().unwrap())
        .collect();
    assert!(!paths.contains(&"srv/a"));
    assert!(paths.contains(&"srv/b"));

    // fetch report again from persisted storage
    let (st, body) = send(&app, get("/rebuild/web")).await;
    assert_eq!(st, StatusCode::OK);
    let again: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(again["rootfsDigest"], rootfs_digest);

    // file provenance lookup
    let (st, body) = send(&app, get("/rebuild/web/file?path=srv/b")).await;
    assert_eq!(st, StatusCode::OK);
    let f: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(f["layerIndex"], 1);

    // missing path -> 404
    let (st, _) = send(&app, get("/rebuild/web/file?path=nope")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);

    // layers endpoint
    let (st, body) = send(&app, get("/rebuild/web/layers")).await;
    assert_eq!(st, StatusCode::OK);
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["layers"].as_array().unwrap().len(), 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn malicious_image_returns_422_and_no_publish() {
    let tmp = tmp_workdir();
    let (app, w) = app(&tmp);
    build_image(
        &w.images_dir(),
        "evil",
        vec![Layer::new(vec![RawEntry::traversal_file("../pwned", b"x")])],
        None,
    );
    let (st, body) = send(&app, post("/rebuild/evil")).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["error"], "unprocessable");

    let (st, _) = send(&app, get("/rebuild/evil")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_image_returns_404_and_bad_name_400() {
    let tmp = tmp_workdir();
    let (app, _w) = app(&tmp);
    let (st, _) = send(&app, post("/rebuild/ghost")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);

    // path traversal in image name must not reach the filesystem
    let req = Request::builder()
        .method("POST")
        .uri("/rebuild/..%2f..%2fetc")
        .body(Body::empty())
        .unwrap();
    let (st, _) = send(&app, req).await;
    assert_ne!(st, StatusCode::OK);
}
