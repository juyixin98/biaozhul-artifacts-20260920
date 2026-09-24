//! End-to-end HTTP tests against the real router (no network socket).

use axum::body::Body;
use axum::http::Request;
use axum::http::StatusCode;
use axum::Router;
use serde_json::{json, Value};
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use tempfile::TempDir;
use tower::ServiceExt;

use build_output_merge::api;

fn router(base: &Path) -> Router {
    api::app(base.to_path_buf())
}

async fn post_json(app: Router, body: Value) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri("/merge")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        panic!(
            "non-JSON response ({status}): {}",
            String::from_utf8_lossy(&bytes)
        )
    });
    (status, v)
}

fn file_out(path: &str, content: &str) -> Value {
    json!({ "path": path, "kind": "file", "content": content })
}
fn dir_out(path: &str) -> Value {
    json!({ "path": path, "kind": "dir" })
}
fn link_out(path: &str, target: &str) -> Value {
    json!({ "path": path, "kind": "symlink", "link_target": target })
}

fn req(actions: Value, extra: Value) -> Value {
    let mut v = json!({ "actions": actions });
    if let (Some(root), Some(obj)) = (v.as_object_mut(), extra.as_object()) {
        for (k, val) in obj {
            root.insert(k.clone(), val.clone());
        }
    }
    v
}

fn find_conflict<'a>(v: &'a Value, kind: &str) -> Option<&'a Value> {
    v["conflicts"]
        .as_array()
        .expect("conflicts array")
        .iter()
        .find(|c| c["kind"] == kind)
}

// ----------------------------------------------------------------- tests

#[tokio::test]
async fn health_ok() {
    let dir = TempDir::new().unwrap();
    let app = router(dir.path());
    let req = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn simple_merge_applies_and_dedupes_identical_content() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "build-x", "outputs": [file_out("x/a.txt", "hello"), file_out("shared/p", "PAYLOAD")]},
            {"id": "build-y", "outputs": [file_out("y/b.txt", "world"), file_out("shared/q", "PAYLOAD"), dir_out("y/sub")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["status"], "applied");
    assert_eq!(v["summary"]["shared_content_groups"], 1);

    let id = v["merge_id"].as_str().unwrap();
    let root = dir.path().join(id);
    assert_eq!(std::fs::read(root.join("x/a.txt")).unwrap(), b"hello");
    assert_eq!(std::fs::read(root.join("y/b.txt")).unwrap(), b"world");
    assert!(root.join("y/sub").is_dir());

    // Identical payload stored once and hard-linked.
    let p1 = std::fs::metadata(root.join("shared/p")).unwrap();
    let p2 = std::fs::metadata(root.join("shared/q")).unwrap();
    assert_eq!(
        p1.ino(),
        p2.ino(),
        "same-content files should share one inode via hard link"
    );
}

#[tokio::test]
async fn file_vs_dir_conflict_reports_actions_and_shortest_path() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("a", "data")]},
            {"id": "act-b", "outputs": [file_out("a/b/deep.txt", "x")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::CONFLICT, "{v}");
    let c = find_conflict(&v, "ancestor_blocking").expect("ancestor_blocking");
    assert_eq!(c["path"], "a");
    assert_eq!(c["other_paths"][0], "a/b/deep.txt");
    let actions = c["actions"].as_array().unwrap();
    assert!(actions.iter().any(|a| a == "act-a"));
    assert!(actions.iter().any(|a| a == "act-b"));

    // Target tree untouched: no merge directory created at all.
    let entries: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
    assert!(
        entries.is_empty(),
        "base dir should stay empty: {entries:?}"
    );
}

#[tokio::test]
async fn same_path_different_content_is_conflict() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("out.txt", "one")]},
            {"id": "act-b", "outputs": [file_out("out.txt", "two")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let c = find_conflict(&v, "same_path_incompatible").expect("same_path_incompatible");
    assert_eq!(c["path"], "out.txt");
    assert_eq!(c["actions"], json!(["act-a", "act-b"]));
    assert!(c["detail"].as_str().unwrap().contains("distinct contents"));
}

#[tokio::test]
async fn same_path_same_content_is_accepted_and_shared() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("same.txt", "identical")]},
            {"id": "act-b", "outputs": [file_out("same.txt", "identical")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["status"], "applied");
}

#[tokio::test]
async fn case_fold_collision_is_detected_by_default() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("Readme", "x")]},
            {"id": "act-b", "outputs": [file_out("README", "y")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let c = find_conflict(&v, "case_fold_collision").expect("case_fold_collision");
    // equal-length names -> lexicographically smallest is the conflict path
    assert_eq!(c["path"], "README");
    assert_eq!(c["other_paths"][0], "Readme");
    assert_eq!(c["actions"], json!(["act-a", "act-b"]));
}

#[tokio::test]
async fn case_fold_can_be_disabled() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("Readme", "x")]},
            {"id": "act-b", "outputs": [file_out("README", "y")]}
        ]),
        json!({"case_sensitive": true}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::OK, "{v}");
}

#[tokio::test]
async fn case_fold_ancestor_blocking_is_flagged() {
    let dir = TempDir::new().unwrap();
    // File "A" must become a directory because "a/b" sits below it when
    // case-folded — exact paths differ, so this is not a plain ancestor.
    let body = req(
        json!([
            {"id": "act-a", "outputs": [file_out("A", "x")]},
            {"id": "act-b", "outputs": [file_out("a/b", "y")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(
        find_conflict(&v, "case_fold_ancestor").is_some(),
        "expected case_fold_ancestor in {v}"
    );
}

#[tokio::test]
async fn symlink_escaping_root_is_conflict() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [link_out("link", "../../etc/passwd")]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let c = find_conflict(&v, "unsafe_link_target").expect("unsafe_link_target");
    assert_eq!(c["path"], "link");
    assert_eq!(c["actions"], json!(["act-a"]));
}

#[tokio::test]
async fn safe_relative_symlink_is_applied() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([
            {"id": "act-a", "outputs": [
                file_out("dir/target.txt", "t"),
                link_out("dir/link", "target.txt")
            ]}
        ]),
        json!({}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let id = v["merge_id"].as_str().unwrap();
    let link = dir.path().join(id).join("dir/link");
    assert_eq!(
        std::fs::read_link(&link).unwrap().to_string_lossy(),
        "target.txt"
    );
}

#[tokio::test]
async fn dry_run_writes_nothing() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([{"id": "act-a", "outputs": [file_out("a", "x")]}]),
        json!({"dry_run": true}),
    );
    let (status, v) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["status"], "planned");
    let entries: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
    assert!(entries.is_empty());
}

#[tokio::test]
async fn invalid_path_is_400() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([{"id": "act-a", "outputs": [file_out("../escape", "x")]}]),
        json!({}),
    );
    let (status, _) = post_json(router(dir.path()), body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let entries: Vec<_> = std::fs::read_dir(dir.path()).unwrap().flatten().collect();
    assert!(entries.is_empty());
}

#[tokio::test]
async fn duplicate_explicit_merge_id_is_409_on_replay() {
    let dir = TempDir::new().unwrap();
    let make = || {
        req(
            json!([{"id": "act-a", "outputs": [file_out("a", "x")]}]),
            json!({"merge_id": "fixed-id"}),
        )
    };
    let (s1, v1) = post_json(router(dir.path()), make()).await;
    assert_eq!(s1, StatusCode::OK, "{v1}");
    let (s2, v2) = post_json(router(dir.path()), make()).await;
    assert_eq!(s2, StatusCode::CONFLICT, "{v2}");
}

#[tokio::test]
async fn get_merge_after_apply() {
    let dir = TempDir::new().unwrap();
    let body = req(
        json!([{"id": "act-a", "outputs": [file_out("a", "x"), dir_out("d")]}]),
        json!({}),
    );
    let app = router(dir.path());
    let (s, v) = post_json(app.clone(), body).await;
    assert_eq!(s, StatusCode::OK);
    let id = v["merge_id"].as_str().unwrap().to_string();

    let req = Request::builder()
        .method("GET")
        .uri(format!("/merge/{id}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let got: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(got["merge_id"], id);
}
