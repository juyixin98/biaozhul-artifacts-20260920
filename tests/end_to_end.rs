//! End-to-end integration tests.
//!
//! These build real in-memory repositories with SHA-256-correct content
//! (except where corruption/cycles are the point) and exercise the public
//! `selector::select` entry point and the Axum router over real TCP-less
//! in-process requests.

use std::sync::Arc;

use manifest_selector::api::build_app;
use manifest_selector::fixtures;
use manifest_selector::model::{Descriptor, ImageManifest, IndexManifest, MT_IMAGE, MT_INDEX};
use manifest_selector::selector::{self, PlatformQuery, SelectError};
use manifest_selector::store::Registry;
use manifest_selector::digest::Digest;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

// ---------- small builder, mirrors fixtures but lets tests craft graphs --

struct Builder<'a> {
    reg: &'a Registry,
}

impl<'a> Builder<'a> {
    fn image(&self, name: &str) -> Descriptor {
        let cfg = serde_json::json!({"architecture":"x","os":"linux","name":name})
            .to_string()
            .into_bytes();
        let cfg_d = self.reg.put_verified(&Digest::of_bytes(&cfg).to_string(), cfg).unwrap();
        let layer = format!("layer-{name}").into_bytes();
        let layer_d = self
            .reg
            .put_verified(&Digest::of_bytes(&layer).to_string(), layer)
            .unwrap();
        let m = ImageManifest {
            schema_version: 2,
            media_type: Some(MT_IMAGE.to_string()),
            config: Descriptor {
                media_type: Some("application/vnd.oci.image.config.v1+json".into()),
                digest: cfg_d.to_string(),
                size: self.reg.get_blob(&cfg_d.to_string()).unwrap().len() as i64,
                urls: vec![],
                platform: None,
                annotations: None,
            },
            layers: vec![Descriptor {
                media_type: Some("application/vnd.oci.image.layer.v1.tar+gzip".into()),
                digest: layer_d.to_string(),
                size: self.reg.get_blob(&layer_d.to_string()).unwrap().len() as i64,
                urls: vec![],
                platform: None,
                annotations: None,
            }],
        };
        let raw = serde_json::to_vec(&m).unwrap();
        let d = self
            .reg
            .put_verified(&Digest::of_bytes(&raw).to_string(), raw.clone())
            .unwrap();
        Descriptor {
            media_type: Some(MT_IMAGE.to_string()),
            digest: d.to_string(),
            size: raw.len() as i64,
            urls: vec![],
            platform: None,
            annotations: None,
        }
    }

    fn index(&self, mut entries: Vec<Descriptor>) -> (String, Descriptor) {
        // Shuffle deterministically? Callers choose order; we also reverse
        // inside specific tests.
        let idx = IndexManifest {
            schema_version: 2,
            media_type: Some(MT_INDEX.to_string()),
            manifests: std::mem::take(&mut entries),
        };
        let raw = serde_json::to_vec(&idx).unwrap();
        let d = self
            .reg
            .put_verified(&Digest::of_bytes(&raw).to_string(), raw.clone())
            .unwrap()
            .to_string();
        (
            d.clone(),
            Descriptor {
                media_type: Some(MT_INDEX.to_string()),
                digest: d,
                size: raw.len() as i64,
                urls: vec![],
                platform: None,
                annotations: None,
            },
        )
    }
}

fn plat(d: Descriptor, os: &str, arch: &str, variant: Option<&str>) -> Descriptor {
    let mut d = d;
    d.platform = Some(manifest_selector::model::Platform {
        architecture: arch.into(),
        os: os.into(),
        variant: variant.map(str::to_string),
        os_version: None,
        os_features: vec![],
        features: vec![],
    });
    d
}

fn q(os: &str, arch: &str, variant: Option<&str>) -> PlatformQuery {
    PlatformQuery {
        os: os.into(),
        architecture: arch.into(),
        variant: variant.map(str::to_string),
        ..Default::default()
    }
}

// ---------- acceptance: ARM variants --------------------------------------

#[test]
fn selects_arm_v7_exactly_and_reports_nested_path() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let v5 = plat(b.image("v5"), "linux", "arm", Some("v5"));
    let v6 = plat(b.image("v6"), "linux", "arm", Some("v6"));
    let v7 = plat(b.image("v7"), "linux", "arm", Some("v7"));
    let amd64 = plat(b.image("amd64"), "linux", "amd64", None);
    let (sub_d, sub_desc) = b.index(vec![v5, v6, v7]);
    let (root, _) = b.index(vec![sub_desc, amd64]);
    reg.tag("r/app", "latest", root.clone());

    let sel = selector::select(&reg, "r/app:latest", &q("linux", "arm", Some("v7")), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().variant.as_deref(), Some("v7"));
    // path went root(index) -> child 0 (nested index) -> child 2 (v7)
    let hops = &sel.selected.path;
    assert_eq!(hops.len(), 2, "path must show both hops: {hops:?}");
    assert_eq!(hops[0].index, 0);
    assert_eq!(hops[0].digest, sub_d);
    assert_eq!(hops[1].index, 2);
}

#[test]
fn arm_v7_request_falls_back_to_v6_when_no_v7() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let v6 = plat(b.image("v6"), "linux", "arm", Some("v6"));
    let (root, _) = b.index(vec![v6]);
    reg.tag("r/arm", "1", root);

    let sel = selector::select(&reg, "r/arm:1", &q("linux", "arm", Some("v7")), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().variant.as_deref(), Some("v6"));
    assert_eq!(sel.selected.score.variant, 1, "fallback, not exact");
}

#[test]
fn arm64_without_variant_matches_v8() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let arm64 = plat(b.image("arm64"), "linux", "arm64", Some("v8"));
    let (root, _) = b.index(vec![arm64]);
    reg.tag("r/a64", "1", root);
    let sel = selector::select(&reg, "r/a64:1", &q("linux", "arm64", None), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().architecture, "arm64");
}

// ---------- acceptance: missing platform ----------------------------------

#[test]
fn missing_platform_is_a_clear_nomatch_error() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let arm64 = plat(b.image("arm64"), "linux", "arm64", Some("v8"));
    let amd64 = plat(b.image("amd64"), "linux", "amd64", None);
    let (root, _) = b.index(vec![arm64, amd64]);
    reg.tag("r/miss", "1", root);

    let err = selector::select(&reg, "r/miss:1", &q("linux", "riscv64", None), true).unwrap_err();
    assert!(matches!(err, SelectError::NoMatch { .. }), "got {err:?}");
    assert!(err.to_string().contains("riscv64"));
}

// ---------- acceptance: same-condition multiple candidates ----------------

#[test]
fn identical_conditions_are_ambiguous_not_first_wins() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let a = plat(b.image("a"), "linux", "amd64", None);
    let c = plat(b.image("c"), "linux", "amd64", None);

    // Order 1: a before c
    let (root1, _) = b.index(vec![a.clone(), c.clone()]);
    reg.tag("r/amb", "o1", root1);
    let err1 = selector::select(&reg, "r/amb:o1", &q("linux", "amd64", None), true).unwrap_err();
    match err1 {
        SelectError::Ambiguous { count, .. } => assert_eq!(count, 2),
        other => panic!("expected Ambiguous, got {other:?}"),
    }

    // Order 2 reversed — same AMBIGUOUS, proving order independence.
    let (root2, _) = b.index(vec![c, a]);
    reg.tag("r/amb", "o2", root2);
    let err2 = selector::select(&reg, "r/amb:o2", &q("linux", "amd64", None), true).unwrap_err();
    assert!(matches!(err2, SelectError::Ambiguous { count: 2, .. }));
}

#[test]
fn variant_disambiguates_same_os_arch() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let v6 = plat(b.image("v6"), "linux", "arm", Some("v6"));
    let v7 = plat(b.image("v7"), "linux", "arm", Some("v7"));
    let (root, _) = b.index(vec![v6, v7]);
    reg.tag("r/d", "1", root);
    let sel = selector::select(&reg, "r/d:1", &q("linux", "arm", Some("v7")), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().variant.as_deref(), Some("v7"));
}

// ---------- acceptance: sub-manifest cycle --------------------------------

#[test]
fn index_cycle_is_detected_with_path() {
    let reg = Registry::new();
    let da = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_string();
    let db = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".to_string();
    let point = |to: &str, size: i64| Descriptor {
        media_type: Some(MT_INDEX.to_string()),
        digest: to.to_string(),
        size,
        urls: vec![],
        platform: None,
        annotations: None,
    };
    let a_bytes = serde_json::to_vec(&IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests: vec![point(&db, 999)],
    })
    .unwrap();
    let b_bytes = serde_json::to_vec(&IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests: vec![point(&da, a_bytes.len() as i64)],
    })
    .unwrap();
    reg.put_trusted(da.clone(), a_bytes, true);
    reg.put_trusted(db.clone(), b_bytes, true);
    reg.tag("r/cyc", "latest", da.clone());

    let err = selector::select(&reg, "r/cyc:latest", &q("linux", "amd64", None), false).unwrap_err();
    match err {
        SelectError::Cycle(chain) => {
            assert!(chain.contains(&da), "chain: {chain}");
            assert!(chain.matches("sha256:").count() >= 3, "chain must repeat a node: {chain}");
        }
        other => panic!("expected Cycle, got {other:?}"),
    }
}

// ---------- digest verification -------------------------------------------

#[test]
fn tampered_leaf_fails_verification_not_selection() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let mut leaf = plat(b.image("leaf"), "linux", "amd64", None);
    let claimed = leaf.digest.clone();
    // Mount garbage under the same digest (simulates storage tampering).
    reg.put_trusted(claimed.clone(), b"{}".to_vec(), false);
    leaf.size = 2;
    let (root, _) = b.index(vec![leaf]);
    reg.tag("r/t", "1", root);

    let err = selector::select(&reg, "r/t:1", &q("linux", "amd64", None), true).unwrap_err();
    assert!(matches!(err, SelectError::Verification { .. }), "got {err:?}");
}

#[test]
fn missing_blob_is_reported() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let mut d = plat(b.image("ghost"), "linux", "amd64", None);
    d.digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000".into();
    // Parent index itself must be valid; its child blob is absent.
    let idx = IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests: vec![d],
    };
    // Can't put_verified (it would hash fine as an index but child lookups
    // happen at walk time), so compute and store.
    let raw = serde_json::to_vec(&idx).unwrap();
    let root = reg.put_verified(&Digest::of_bytes(&raw).to_string(), raw).unwrap();
    reg.tag("r/g", "1", root.to_string());
    let err = selector::select(&reg, "r/g:1", &q("linux", "amd64", None), true).unwrap_err();
    assert!(matches!(err, SelectError::BlobMissing { .. }), "got {err:?}");
}

#[test]
fn direct_image_reference_is_returned() {
    let reg = Registry::new();
    let b = Builder { reg: &reg };
    let d = b.image("direct");
    reg.tag("r/direct", "1", d.digest.clone());
    let sel = selector::select(&reg, "r/direct:1", &q("linux", "amd64", None), true).unwrap();
    assert_eq!(sel.selected.digest, d.digest);
}

// ---------- built-in fixtures cover every scenario ------------------------

#[test]
fn builtin_fixtures_behave_as_documented() {
    let reg = Registry::new();
    fixtures::build_all(&reg);

    // arm variant through nested index
    let sel = selector::select(&reg, "demo/app:latest", &q("linux", "arm", Some("v7")), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().variant.as_deref(), Some("v7"));

    // arm64
    let sel = selector::select(&reg, "demo/app:latest", &q("linux", "arm64", None), true).unwrap();
    assert_eq!(sel.selected.platform.as_ref().unwrap().architecture, "arm64");

    // missing platform
    let err = selector::select(&reg, "demo/app:latest", &q("linux", "ppc64le", None), true).unwrap_err();
    assert!(matches!(err, SelectError::NoMatch { .. }));

    // ambiguous
    let err = selector::select(&reg, "demo/ambiguous:1", &q("linux", "amd64", None), true).unwrap_err();
    assert!(matches!(err, SelectError::Ambiguous { .. }));

    // corrupt
    let err = selector::select(&reg, "demo/corrupt:1", &q("linux", "amd64", None), true).unwrap_err();
    assert!(matches!(err, SelectError::Verification { .. }), "got {err:?}");

    // cycle (strict=false so trusted-mounted graph is traversed)
    let err = selector::select(&reg, "demo/cycle:latest", &q("linux", "amd64", None), false).unwrap_err();
    assert!(matches!(err, SelectError::Cycle(_)), "got {err:?}");
}

// ---------- HTTP end-to-end via the Axum router ---------------------------

#[tokio::test]
async fn http_select_arm_and_ambiguous_and_cycle() {
    let reg = Arc::new(Registry::new());
    fixtures::build_all(&reg);
    let app = build_app(reg);

    // arm/v7 selection
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v2/demo/app/select")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"reference":"latest","os":"linux","architecture":"arm","variant":"v7"})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    let variant = v["result"]["selected"]["platform"]["variant"].as_str();
    assert_eq!(variant, Some("v7"));
    let path_len = v["result"]["selected"]["path"].as_array().unwrap().len();
    assert!(path_len >= 2, "selection path must be reported");

    // ambiguous -> 409
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v2/demo/ambiguous/select")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"reference":"1","os":"linux","architecture":"amd64"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["errors"][0]["code"], "AMBIGUOUS");

    // missing platform -> 404 NO_MATCH
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v2/demo/app/select")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"reference":"latest","os":"linux","architecture":"ppc64le"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // cycle with strict=false -> 422 MANIFEST_CYCLE
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v2/demo/cycle/select?strict=false")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"reference":"latest","os":"linux","architecture":"amd64"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["errors"][0]["code"], "MANIFEST_CYCLE");
}

#[tokio::test]
async fn http_upload_then_select_roundtrip() {
    let reg = Arc::new(Registry::new());
    let app = build_app(reg.clone());

    // helper closure via macro-like function
    async fn put(app: axum::Router, uri: &str, body: Vec<u8>) -> (StatusCode, serde_json::Value) {
        let resp = app
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri(uri)
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v = serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null);
        (status, v)
    }

    // config blob
    let cfg = br#"{"os":"linux","architecture":"amd64"}"#.to_vec();
    let cfg_digest = Digest::of_bytes(&cfg).to_string();
    let (s, _) = put(
        app.clone(),
        &format!("/v2/u/app/blobs/uploads/?digest={cfg_digest}"),
        cfg,
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);

    // layer blob
    let layer = b"fake-layer".to_vec();
    let layer_digest = Digest::of_bytes(&layer).to_string();
    let (s, _) = put(
        app.clone(),
        &format!("/v2/u/app/blobs/uploads/?digest={layer_digest}"),
        layer,
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);

    // image manifest (tagged)
    let img = serde_json::json!({
        "schemaVersion": 2,
        "mediaType": MT_IMAGE,
        "config": {"mediaType":"application/vnd.oci.image.config.v1+json","digest":cfg_digest,"size":31},
        "layers": [{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip","digest":layer_digest,"size":10}]
    });
    let img_bytes = serde_json::to_vec(&img).unwrap();
    let (s, v) = put(app.clone(), "/v2/u/app/manifests/1.0", img_bytes).await;
    assert_eq!(s, StatusCode::CREATED, "manifest put failed: {v}");
    let manifest_digest = v["digest"].as_str().unwrap().to_string();

    // index pointing at it for linux/amd64
    let idx = serde_json::json!({
        "schemaVersion": 2,
        "mediaType": MT_INDEX,
        "manifests": [{
            "mediaType": MT_IMAGE,
            "digest": manifest_digest,
            "size": reg.get_blob(&manifest_digest).unwrap().len(),
            "platform": {"architecture":"amd64","os":"linux"}
        }]
    });
    let idx_bytes = serde_json::to_vec(&idx).unwrap();
    let (s, _) = put(app.clone(), "/v2/u/app/manifests/latest", idx_bytes).await;
    assert_eq!(s, StatusCode::CREATED);

    // select it
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v2/u/app/select")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"reference":"latest","os":"linux","architecture":"amd64"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn http_rejects_bad_digest_upload() {
    let reg = Arc::new(Registry::new());
    let app = build_app(reg);
    let resp = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri("/v2/x/y/blobs/uploads/?digest=sha256:0000000000000000000000000000000000000000000000000000000000000000")
                .body(Body::from("not matching"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}
