//! End-to-end HTTP tests driving the real Axum router via `oneshot`.

use std::path::Path;

use axum::body::Body;
use axum::Router;
use fixture_maker::{build_image, evil, LayerSpec, RawEntry as E};
use http::Request;
use http_body_util::BodyExt;
use oci_unpack::server::build_router;
use tower::ServiceExt;

struct Harness {
    _dir: tempfile::TempDir,
    fixtures: std::path::PathBuf,
    builds: std::path::PathBuf,
    app: Router,
}

impl Harness {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let fixtures = dir.path().join("fixtures");
        let builds = dir.path().join("builds");
        std::fs::create_dir_all(&fixtures).unwrap();
        let app = build_router(&fixtures, &builds, oci_unpack::Limits::default()).unwrap();
        Harness {
            _dir: dir,
            fixtures,
            builds,
            app,
        }
    }

    fn make(&self, name: &str, layers: &[LayerSpec]) {
        build_image(self.fixtures.join(name), layers).expect("build fixture");
    }

    async fn post(&self, uri: &str) -> (u16, serde_json::Value) {
        self.send(
            Request::builder()
                .method("POST")
                .uri(uri)
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    async fn get(&self, uri: &str) -> (u16, serde_json::Value) {
        self.send(Request::builder().uri(uri).body(Body::empty()).unwrap())
            .await
    }

    async fn send(&self, req: Request<Body>) -> (u16, serde_json::Value) {
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let status = resp.status().as_u16();
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        let value = if bytes.is_empty() {
            serde_json::Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or_else(|e| {
                panic!("invalid JSON ({e}): {}", String::from_utf8_lossy(&bytes))
            })
        };
        (status, value)
    }

    fn published_root_exists(&self, name: &str, rel: &str) -> bool {
        Path::new(&self.builds)
            .join(name)
            .join("latest")
            .join("rootfs")
            .join(rel)
            .exists()
    }
}

#[tokio::test]
async fn health_and_list() {
    let h = Harness::new();
    let (s, v) = h.get("/healthz").await;
    assert_eq!(s, 200);
    assert_eq!(v["status"], "ok");

    h.make(
        "alpha",
        &[LayerSpec::gzip(vec![E::file("f", b"x".to_vec())])],
    );
    let (s, v) = h.get("/v1/images").await;
    assert_eq!(s, 200);
    let names: Vec<&str> = v["images"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["name"].as_str().unwrap())
        .collect();
    assert!(names.contains(&"alpha"));
}

#[tokio::test]
async fn rebuild_success_http() {
    let h = Harness::new();
    h.make(
        "img",
        &[
            LayerSpec::gzip(vec![E::file("a", b"one".to_vec())]),
            LayerSpec::gzip(vec![E::file("a", b"two".to_vec())]),
        ],
    );
    let (s, v) = h.post("/v1/images/img/rebuild").await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["status"], "published");
    assert!(v["final_digest"].as_str().unwrap().starts_with("sha256:"));
    assert_eq!(v["layers"].as_array().unwrap().len(), 2);
    assert!(h.published_root_exists("img", "a"));
}

#[tokio::test]
async fn rebuild_rejected_http_422_and_no_publish() {
    let h = Harness::new();
    h.make("evil", &[LayerSpec::gzip(vec![evil::traversal_escape()])]);
    let (s, v) = h.post("/v1/images/evil/rebuild").await;
    assert_eq!(s, 422);
    assert_eq!(v["error"]["code"], "path_traversal");
    assert!(!Path::new(&h.builds).join("evil").join("latest").exists());
}

#[tokio::test]
async fn rebuild_missing_image_404() {
    let h = Harness::new();
    let (s, v) = h.post("/v1/images/nope/rebuild").await;
    assert_eq!(s, 404);
    assert_eq!(v["error"]["code"], "not_found");
}

#[tokio::test]
async fn rejects_bad_fixture_name() {
    let h = Harness::new();
    let (s, v) = h.post("/v1/images/..%2fetc/rebuild").await;
    // axum rejects the encoded slash path before routing (400), or our
    // validator returns bad_name — both are safe outcomes.
    assert!(s == 400 || s == 404, "status was {s}: {v}");
}

#[tokio::test]
async fn builds_listing_tracks_publish() {
    let h = Harness::new();
    h.make("img", &[LayerSpec::gzip(vec![E::file("a", b"x".to_vec())])]);
    let (s, _) = h.get("/v1/images/img/builds").await;
    assert_eq!(s, 200); // empty list is still a valid response
    let (_, v) = h.post("/v1/images/img/rebuild").await;
    let build_id = v["build_id"].as_str().unwrap().to_string();
    let (s, v) = h.get("/v1/images/img/builds").await;
    assert_eq!(s, 200);
    assert_eq!(v["latest"], build_id);
    assert!(v["builds"].as_array().unwrap()[0].as_str().unwrap().len() == build_id.len());
}
