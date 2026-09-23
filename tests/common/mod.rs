//! Test helpers: drive the real Axum router in-process via `oneshot`.

#![allow(dead_code)]

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use axum::Router;
use cas_gc::store::{Store, StoreConfig};
use serde_json::Value;
use tower::ServiceExt;

pub struct TestApp {
    pub app: Router,
    pub store: Store,
    pub dir: tempfile::TempDir,
}

pub async fn spawn_app() -> TestApp {
    let dir = tempfile::tempdir().unwrap();
    let cfg = StoreConfig {
        default_retention_secs: 3600,
    };
    let store = Store::open(dir.path(), cfg).unwrap();
    let app = cas_gc::api::router(store.clone());
    TestApp { app, store, dir }
}

pub struct Resp {
    pub status: StatusCode,
    pub json: Value,
    pub raw: Vec<u8>,
}

pub async fn send(
    app: &Router,
    method: &str,
    uri: &str,
    content_type: Option<&str>,
    body: Vec<u8>,
) -> Resp {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(ct) = content_type {
        builder = builder.header("content-type", ct);
    }
    let req = builder.body(Body::from(body)).unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let raw = bytes.to_vec();
    let json = serde_json::from_slice(&raw).unwrap_or(Value::Null);
    Resp { status, json, raw }
}

pub async fn post_json(app: &Router, uri: &str, v: Value) -> Resp {
    send(
        app,
        "POST",
        uri,
        Some("application/json"),
        serde_json::to_vec(&v).unwrap(),
    )
    .await
}

pub async fn put_json(app: &Router, uri: &str, v: Value) -> Resp {
    send(
        app,
        "PUT",
        uri,
        Some("application/json"),
        serde_json::to_vec(&v).unwrap(),
    )
    .await
}

pub async fn get(app: &Router, uri: &str) -> Resp {
    send(app, "GET", uri, None, Vec::new()).await
}

pub async fn delete(app: &Router, uri: &str) -> Resp {
    send(app, "DELETE", uri, None, Vec::new()).await
}

pub async fn put_blob(app: &Router, data: &[u8]) -> String {
    let r = send(
        app,
        "POST",
        "/blobs",
        Some("application/octet-stream"),
        data.to_vec(),
    )
    .await;
    assert_eq!(r.status, StatusCode::OK, "put_blob failed: {}", r.json);
    r.json["hash"].as_str().unwrap().to_string()
}

pub async fn put_manifest(app: &Router, v: &Value) -> String {
    let r = post_json(app, "/manifests", v.clone()).await;
    assert_eq!(
        r.status,
        StatusCode::OK,
        "put_manifest failed: {} for {v}",
        r.json
    );
    r.json["hash"].as_str().unwrap().to_string()
}

pub async fn put_root(app: &Router, name: &str, manifest: &str) {
    let r = put_json(
        app,
        &format!("/roots/{name}"),
        serde_json::json!({"manifest": manifest}),
    )
    .await;
    assert_eq!(r.status, StatusCode::OK, "put_root failed: {}", r.json);
}

pub async fn gc(app: &Router) -> Value {
    let r = post_json(app, "/gc", Value::Null).await;
    assert_eq!(r.status, StatusCode::OK);
    r.json
}

pub async fn get_object(app: &Router, hash: &str) -> Resp {
    get(app, &format!("/objects/{hash}")).await
}

pub async fn delete_root(app: &Router, name: &str) -> Value {
    delete(app, &format!("/roots/{name}")).await.json
}

pub async fn reachable(app: &Router) -> std::collections::HashSet<String> {
    let r = get(app, "/reachable").await;
    r.json["reachable"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect()
}
