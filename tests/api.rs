//! 端到端集成测试：直接对 Axum Router 发请求（无需监听端口）。
//!
//! 覆盖验收点：
//! - a 文件 与 a/b 目录冲突，给出来源动作与最短冲突路径
//! - 跨动作同路径异内容 → 内容冲突
//! - 同路径同内容 → 共享，不冲突；落盘后硬链接共享
//! - 大小写折叠碰撞
//! - 符号链接目标规范化与逃逸
//! - 失败不改变目标树；成功后树内容正确
//! - 请求级 400 错误、健康检查、404

use std::fs;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::Engine;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;

use build_merge::http::router;

fn b64(s: &str) -> String {
    base64::engine::general_purpose::STANDARD.encode(s)
}

async fn post(path: &str, body: Value) -> (StatusCode, Value) {
    let app = router();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let json: Value = serde_json::from_slice(&bytes).unwrap_or_else(|e| {
        panic!(
            "invalid JSON body: {e}; raw: {}",
            String::from_utf8_lossy(&bytes)
        )
    });
    (status, json)
}

fn file_entry(path: &str, content: &str) -> Value {
    json!({ "path": path, "type": "file", "content_base64": b64(content) })
}
fn dir_entry(path: &str) -> Value {
    json!({ "path": path, "type": "dir" })
}
fn symlink_entry(path: &str, target: &str) -> Value {
    json!({ "path": path, "type": "symlink", "target": target })
}
fn action(id: &str, outputs: Vec<Value>) -> Value {
    json!({ "id": id, "outputs": outputs })
}
fn req(target: &str, actions: Vec<Value>) -> Value {
    json!({ "target_dir": target, "actions": actions })
}

fn first_conflict(plan: &Value) -> &Value {
    plan.get("conflicts")
        .unwrap()
        .as_array()
        .unwrap()
        .first()
        .unwrap()
}

// ---------- 规划：文件 / 目录互斥 ----------

#[tokio::test]
async fn file_a_conflicts_with_nested_a_b() {
    // 同一动作内：文件 a 与目录 a/b
    let r = req(
        "/tmp/ignored",
        vec![action(
            "build",
            vec![file_entry("a", "x"), file_entry("a/b", "y")],
        )],
    );
    let (status, body) = post("/plan", r).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], false);
    let c = first_conflict(&body["plan"]);
    assert_eq!(c["kind"], "file_dir_conflict");
    assert_eq!(c["path"], "a"); // 最短冲突路径
    assert_eq!(c["actions"], json!(["build"]));
}

#[tokio::test]
async fn cross_action_file_vs_dir_conflict_names_both_actions() {
    // 跨动作：act1 产文件 a，act2 产文件 a/b（隐含目录 a）
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![file_entry("a", "file")]),
            action("act2", vec![file_entry("a/b", "nested")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    let c = first_conflict(&body["plan"]);
    assert_eq!(c["kind"], "file_dir_conflict");
    assert_eq!(c["path"], "a");
    let actions = c["actions"].as_array().unwrap();
    assert!(actions.contains(&json!("act1")));
    assert!(actions.contains(&json!("act2")));
}

#[tokio::test]
async fn file_vs_explicit_dir_same_path() {
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![file_entry("a", "x")]),
            action("act2", vec![dir_entry("a")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    assert_eq!(first_conflict(&body["plan"])["kind"], "file_dir_conflict");
}

// ---------- 规划：同路径内容 ----------

#[tokio::test]
async fn same_path_different_content_is_conflict() {
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![file_entry("out/x", "hello")]),
            action("act2", vec![file_entry("out/x", "world")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    let c = first_conflict(&body["plan"]);
    assert_eq!(c["kind"], "content_conflict");
    assert_eq!(c["path"], "out/x");
    assert_eq!(c["actions"], json!(["act1", "act2"]));
}

#[tokio::test]
async fn same_path_same_content_is_shared_without_conflict() {
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![file_entry("out/x", "same")]),
            action("act2", vec![file_entry("out/x", "same")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], true, "body: {body}");
    let f = &body["plan"]["files"]["out/x"];
    assert_eq!(f["actions"], json!(["act1", "act2"]));
    assert_eq!(
        f["sha256"],
        "0967115f2813a3541eaef77de9d9d5773f1c0c04314b0bbfe4ff3b3b1c55b5d5"
    );
}

#[tokio::test]
async fn identical_content_at_different_paths_is_deduped_on_apply() {
    // 不同路径同内容：两个路径都落盘，但内容对象只有一份（硬链接共享）。
    let tmp = tempdir();
    let target = format!("{tmp}/root");
    let r = req(
        &target,
        vec![action(
            "act1",
            vec![file_entry("a", "dup"), file_entry("nested/b", "dup")],
        )],
    );
    let (status, body) = post("/apply", r).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["result"]["files_written"], 2);
    assert_eq!(body["result"]["content_objects"], 1); // 去重对象数

    let meta1 = fs::metadata(format!("{target}/a")).unwrap();
    let meta2 = fs::metadata(format!("{target}/nested/b")).unwrap();
    assert_eq!(meta1.len(), meta2.len());
    assert_eq!(meta1.ino(), meta2.ino(), "expected hardlink-shared inode");
}

// ---------- 大小写折叠 ----------

#[tokio::test]
async fn case_fold_collision_is_detected() {
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![file_entry("Bin/TOOL", "x")]),
            action("act2", vec![file_entry("bin/tool", "y")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    let kinds: Vec<&str> = body["plan"]["conflicts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["kind"].as_str().unwrap())
        .collect();
    assert!(
        kinds.contains(&"case_fold_collision"),
        "expected case collision, got: {kinds:?}"
    );
    let case = body["plan"]["conflicts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["kind"] == "case_fold_collision")
        .unwrap();
    // 最短冲突路径深度为 2（bin/tool 同级），来源包含两个动作。
    assert_eq!(case["actions"].as_array().unwrap().len(), 2);
}

// ---------- 符号链接 ----------

#[tokio::test]
async fn symlink_targets_normalized_and_shared() {
    // x/./y 与 x/y 规范化后相同 → 视为同目标，不冲突。
    let r = req(
        "/tmp/ignored",
        vec![
            action("act1", vec![symlink_entry("a/link", "x/./y")]),
            action("act2", vec![symlink_entry("a/link", "x/y")]),
        ],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], true, "body: {body}");
    assert_eq!(body["plan"]["symlinks"]["a/link"]["target"], "x/y");
}

#[tokio::test]
async fn symlink_absolute_target_rejected() {
    let r = req(
        "/tmp/ignored",
        vec![action("act1", vec![symlink_entry("a/link", "/etc/passwd")])],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    assert_eq!(first_conflict(&body["plan"])["kind"], "symlink_escape");
}

#[tokio::test]
async fn symlink_escaping_root_rejected() {
    let r = req(
        "/tmp/ignored",
        vec![action("act1", vec![symlink_entry("a/link", "../../x")])],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    assert_eq!(first_conflict(&body["plan"])["kind"], "symlink_escape");
}

#[tokio::test]
async fn legal_parent_traversal_normalized() {
    // 链接在 a/b/link，目标 ../../x 解析到输出根下 x：合法且保留 ..。
    let r = req(
        "/tmp/ignored",
        vec![action("act1", vec![symlink_entry("a/b/link", "../../x")])],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], true, "body: {body}");
    assert_eq!(body["plan"]["symlinks"]["a/b/link"]["target"], "../../x");
}

// ---------- apply 原子性 ----------

#[tokio::test]
async fn apply_conflict_does_not_touch_target() {
    let tmp = tempdir();
    let target = format!("{tmp}/root");
    fs::create_dir_all(&target).unwrap();
    fs::write(format!("{target}/keep.txt"), "untouched").unwrap();

    let r = req(
        &target,
        vec![
            action("act1", vec![file_entry("a", "one")]),
            action("act2", vec![file_entry("a", "two")]),
        ],
    );
    let (status, body) = post("/apply", r).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["ok"], false);
    // 目标树原样保留，且没有混入新文件 / 临时目录。
    assert_eq!(
        fs::read_to_string(format!("{target}/keep.txt")).unwrap(),
        "untouched"
    );
    let names: Vec<String> = fs::read_dir(&target)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(
        names,
        vec!["keep.txt".to_string()],
        "target must be unchanged: {names:?}"
    );

    // 父目录也不能残留临时目录。
    let parents: Vec<String> = fs::read_dir(&tmp)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert!(
        parents.iter().all(|n| !n.contains("build-merge")),
        "temp dirs leaked: {parents:?}"
    );
}

#[tokio::test]
async fn apply_writes_complete_tree_and_replaces_existing() {
    let tmp = tempdir();
    let target = format!("{tmp}/root");

    // 第一次应用：act1 + act2 合并（同内容共享）。
    let r = req(
        &target,
        vec![
            action(
                "act1",
                vec![
                    dir_entry("bin"),
                    file_entry("bin/tool", "ELF"),
                    symlink_entry("bin/current", "tool"),
                ],
            ),
            action(
                "act2",
                vec![file_entry("bin/tool", "ELF"), file_entry("README", "hi")],
            ),
        ],
    );
    let (status, body) = post("/apply", r).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["result"]["dirs_created"], 1); // 只有显式 bin；bin/current 等的父目录已存在
    assert_eq!(
        fs::read_to_string(format!("{target}/bin/tool")).unwrap(),
        "ELF"
    );
    assert_eq!(
        fs::read_to_string(format!("{target}/README")).unwrap(),
        "hi"
    );

    #[cfg(unix)]
    {
        let dst = fs::read_link(format!("{target}/bin/current")).unwrap();
        assert_eq!(dst, std::path::Path::new("tool"));
    }

    // 第二次应用：整体替换旧树，旧文件应消失。
    let r2 = req(
        &target,
        vec![action("act3", vec![file_entry("only.txt", "new")])],
    );
    let (_, body2) = post("/apply", r2).await;
    assert_eq!(body2["ok"], true);
    assert_eq!(
        fs::read_to_string(format!("{target}/only.txt")).unwrap(),
        "new"
    );
    assert!(fs::metadata(format!("{target}/README")).is_err());
    assert!(fs::metadata(format!("{target}/bin")).is_err());
}

// ---------- 请求级错误 / 杂项 ----------

#[tokio::test]
async fn invalid_path_and_bad_base64_are_reported() {
    let r = req(
        "/tmp/ignored",
        vec![action(
            "act1",
            vec![
                json!({ "path": "../escape", "type": "file", "content_base64": b64("x") }),
                json!({ "path": "bad", "type": "file", "content_base64": "!!!not-base64" }),
            ],
        )],
    );
    let (_, body) = post("/plan", r).await;
    assert_eq!(body["ok"], false);
    let kinds: Vec<String> = body["plan"]["conflicts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["kind"].as_str().unwrap().to_string())
        .collect();
    assert!(kinds.iter().all(|k| k == "invalid_path"), "got {kinds:?}");
}

#[tokio::test]
async fn empty_actions_and_duplicate_ids_are_400() {
    let (s1, b1) = post("/plan", req("/tmp/x", vec![])).await;
    assert_eq!(s1, StatusCode::BAD_REQUEST);
    assert!(b1["error"].as_str().unwrap().contains("empty"));

    let (s2, b2) = post(
        "/plan",
        req("/tmp/x", vec![action("a", vec![]), action("a", vec![])]),
    )
    .await;
    assert_eq!(s2, StatusCode::BAD_REQUEST);
    assert!(b2["error"].as_str().unwrap().contains("duplicate"));
}

#[tokio::test]
async fn health_and_404() {
    let app = router();
    let resp = app
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);

    let app = router();
    let resp = app
        .oneshot(Request::builder().uri("/nope").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

// ---------- 辅助 ----------

fn tempdir() -> String {
    let dir = std::env::temp_dir().join(format!(
        "build-merge-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir.to_string_lossy().into_owned()
}

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
