//! 集成测试：验收标准对应的自动化验证。
//!
//! 覆盖：
//! 1. 相同内容 → 相同包字节（重复打包）；
//! 2. 改变文件创建顺序（目录枚举顺序）→ 字节不变；
//! 3. 改变文件 mtime → 字节不变；
//! 4. 冲突规范路径 → 拒绝（409）；
//! 5. 符号链接规则（根内允许、逃逸拒绝、绝对目标拒绝）；
//! 6. tar 输出可被标准 tar 解析且元数据符合规范；
//! 7. HTTP 接口端到端（manifest JSON / tar 下载）。

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use repro_pack::normalize::normalize_path;
use repro_pack::pack::{pack, PackError};
use std::fs;
use std::path::Path;
use std::time::SystemTime;
use tempfile::TempDir;
use tower::util::ServiceExt;

/// 构造固定内容的测试目录。
fn fixture(dir: &Path) {
    fs::create_dir_all(dir.join("src/nested")).unwrap();
    fs::write(dir.join("README.md"), b"hello reproducible world\n").unwrap();
    fs::write(dir.join("src/main.rs"), b"fn main() {}\n").unwrap();
    fs::write(dir.join("src/nested/data.bin"), &[0u8, 1, 2, 3, 255]).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(dir.join("src/main.rs"), fs::Permissions::from_mode(0o755)).unwrap();
    }
}

/// 按相反顺序创建同样的内容（模拟不同枚举顺序）。
fn fixture_reversed(dir: &Path) {
    fs::create_dir_all(dir.join("src/nested")).unwrap();
    fs::write(dir.join("src/nested/data.bin"), &[0u8, 1, 2, 3, 255]).unwrap();
    fs::write(dir.join("src/main.rs"), b"fn main() {}\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(dir.join("src/main.rs"), fs::Permissions::from_mode(0o755)).unwrap();
    }
    fs::write(dir.join("README.md"), b"hello reproducible world\n").unwrap();
}

#[test]
fn same_content_same_bytes() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    let a = pack(t.path(), None).unwrap();
    let b = pack(t.path(), None).unwrap();
    assert_eq!(a.tar_bytes, b.tar_bytes, "重复打包字节必须一致");
    assert_eq!(a.manifest.archive.sha256, b.manifest.archive.sha256);
}

#[test]
fn enumeration_order_does_not_matter() {
    let t1 = TempDir::new().unwrap();
    let t2 = TempDir::new().unwrap();
    fixture(t1.path());
    fixture_reversed(t2.path());
    let a = pack(t1.path(), None).unwrap();
    let b = pack(t2.path(), None).unwrap();
    assert_eq!(
        a.manifest.archive.sha256, b.manifest.archive.sha256,
        "创建/枚举顺序不同但内容相同，摘要必须一致"
    );
    assert_eq!(a.tar_bytes, b.tar_bytes);
}

#[test]
fn mtime_does_not_matter() {
    let t1 = TempDir::new().unwrap();
    let t2 = TempDir::new().unwrap();
    fixture(t1.path());
    fixture(t2.path());
    // 把 t2 所有文件 mtime 改成完全不同的值
    for entry in ["README.md", "src/main.rs", "src/nested/data.bin"] {
        let f = fs::File::options().write(true).open(t2.path().join(entry)).unwrap();
        f.set_modified(SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000))
            .unwrap();
    }
    let a = pack(t1.path(), None).unwrap();
    let b = pack(t2.path(), None).unwrap();
    assert_eq!(a.tar_bytes, b.tar_bytes, "mtime 不同但内容相同，字节必须一致");
}

#[test]
fn conflicting_normalized_paths_rejected() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    // "src//main.rs"、"src/./main.rs"、"src/main.rs" 规范化为同一路径
    let paths = vec![
        "src//main.rs".to_string(),
        "src/./main.rs".to_string(),
    ];
    let err = pack(t.path(), Some(&paths)).unwrap_err();
    assert!(
        matches!(err, PackError::Conflict(ref p) if p == "src/main.rs"),
        "期望冲突错误，得到: {err:?}"
    );
}

#[test]
fn duplicate_explicit_path_rejected() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    let paths = vec!["README.md".to_string(), "README.md".to_string()];
    let err = pack(t.path(), Some(&paths)).unwrap_err();
    assert!(matches!(err, PackError::Conflict(_)));
}

#[test]
fn path_traversal_rejected() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    for bad in ["../escape", "/abs/path", "a/../../b", ""] {
        let paths = vec![bad.to_string()];
        assert!(pack(t.path(), Some(&paths)).is_err(), "应拒绝: {bad:?}");
    }
    assert!(normalize_path("a//b").is_ok());
}

#[cfg(unix)]
#[test]
fn symlink_rules() {
    use std::os::unix::fs::symlink;
    // 根内相对链接：允许，目标被规范化
    let t = TempDir::new().unwrap();
    fixture(t.path());
    symlink("../README.md", t.path().join("src/readme-link")).unwrap();
    let r = pack(t.path(), None).unwrap();
    let link = r
        .manifest
        .entries
        .iter()
        .find(|e| e.path == "src/readme-link")
        .expect("链接应出现在清单中");
    assert_eq!(link.kind, repro_pack::pack::Kind::Symlink);
    assert_eq!(link.link_target.as_deref(), Some("README.md"));

    // 逃逸根目录的链接：拒绝
    let t2 = TempDir::new().unwrap();
    fixture(t2.path());
    symlink("../../../etc/passwd", t2.path().join("evil")).unwrap();
    assert!(pack(t2.path(), None).is_err());

    // 绝对目标链接：拒绝
    let t3 = TempDir::new().unwrap();
    fixture(t3.path());
    symlink("/etc/hostname", t3.path().join("abs-link")).unwrap();
    assert!(pack(t3.path(), None).is_err());
}

#[test]
fn tar_output_parseable_and_normalized() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    let r = pack(t.path(), None).unwrap();

    let mut ar = tar::Archive::new(&r.tar_bytes[..]);
    let mut names = Vec::new();
    for e in ar.entries().unwrap() {
        let e = e.unwrap();
        let h = e.header();
        assert_eq!(h.mtime().unwrap(), 0, "mtime 必须固定为 0");
        assert_eq!(h.uid().unwrap(), 0);
        assert_eq!(h.gid().unwrap(), 0);
        let path = e.path().unwrap().to_string_lossy().into_owned();
        let mode = h.mode().unwrap();
        if path == "src/main.rs" {
            assert_eq!(mode, 0o755, "可执行文件映射为 0755");
        }
        if path == "README.md" {
            assert_eq!(mode, 0o644, "普通文件映射为 0644");
        }
        names.push(path);
    }
    // 顺序必须按字节序排好
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted, "tar 条目必须按路径字节序排列");
    assert!(names.contains(&"README.md".to_string()));
    assert!(names.contains(&"src/nested/data.bin".to_string()));
}

#[test]
fn manifest_lists_all_entries_with_digests() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    let r = pack(t.path(), None).unwrap();
    assert_eq!(r.manifest.format, "repro-pack/1");
    assert_eq!(r.manifest.archive.size_bytes, r.tar_bytes.len() as u64);
    let readme = r
        .manifest
        .entries
        .iter()
        .find(|e| e.path == "README.md")
        .unwrap();
    assert_eq!(readme.mode, "0644");
    assert_eq!(readme.size, Some(25));
    assert!(readme.sha256.as_deref().unwrap().len() == 64);
}

// ---------- HTTP 端到端 ----------

fn json_req(body: serde_json::Value) -> Request<Body> {
    Request::post("/pack")
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap()
}

#[tokio::test]
async fn http_pack_manifest_and_tar() {
    let t = TempDir::new().unwrap();
    fixture(t.path());
    let app = repro_pack::server::build_router();

    // 1) JSON 清单
    let resp = app
        .clone()
        .oneshot(json_req(serde_json::json!({"root": t.path().to_str().unwrap()})))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let manifest: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let sha = manifest["archive"]["sha256"].as_str().unwrap().to_string();
    assert_eq!(sha.len(), 64);

    // 2) tar 下载，头部摘要与清单一致
    let resp = app
        .clone()
        .oneshot(
            Request::post("/pack")
                .header("content-type", "application/json")
                .header("accept", "application/x-tar")
                .body(Body::from(
                    serde_json::to_vec(&serde_json::json!({"root": t.path().to_str().unwrap()}))
                        .unwrap(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(
        resp.headers().get("x-archive-sha256").unwrap().to_str().unwrap(),
        sha
    );
    let tar_bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    // 本地再算一次摘要对比
    let local = pack(t.path(), None).unwrap();
    assert_eq!(&tar_bytes[..], &local.tar_bytes[..]);

    // 3) 冲突路径 → 409
    let resp = app
        .clone()
        .oneshot(json_req(serde_json::json!({
            "root": t.path().to_str().unwrap(),
            "paths": ["src//main.rs", "src/./main.rs"]
        })))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let err: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert!(err["error"].as_str().unwrap().contains("conflicting"));

    // 4) 不存在的目录 → 404
    let resp = app
        .oneshot(json_req(serde_json::json!({"root": "/no/such/dir/xyz"})))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}
