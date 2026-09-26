//! Black-box tests of the `ifix` binary and its JSON control surface.

mod common;

use common::sample_tree;
use serde_json::json;
use std::process::Command;

fn bin() -> Command {
    Command::new(env!("CARGO_BIN_EXE_ifix"))
}

fn unique_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "ifix-cli-{name}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn cli_build_verify_lookup_read_roundtrip() {
    let dir = unique_dir("roundtrip");
    let request_path = dir.join("tree.json");
    let index_path = dir.join("out.ifix");

    let request = json!({
        "output": index_path.to_string_lossy(),
        "log2_page": 6,
        "root": sample_tree()
    });
    std::fs::write(&request_path, serde_json::to_vec_pretty(&request).unwrap()).unwrap();

    let build = bin()
        .arg("build")
        .arg("--request")
        .arg(&request_path)
        .output()
        .unwrap();
    assert!(
        build.status.success(),
        "build failed: {}",
        String::from_utf8_lossy(&build.stdout)
    );
    let build_json: serde_json::Value = serde_json::from_slice(&build.stdout).unwrap();
    assert_eq!(build_json["status"], "ok");
    assert!(build_json["data"]["file_len"].as_u64().unwrap() > 0);

    // verify on both backends
    for backend in ["read", "mmap"] {
        let out = bin()
            .args(["verify", "--index"])
            .arg(&index_path)
            .arg("--backend")
            .arg(backend)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "verify/{backend}: {}",
            String::from_utf8_lossy(&out.stdout)
        );
    }

    // lookup a file
    let out = bin()
        .args(["lookup", "--index"])
        .arg(&index_path)
        .args(["--path", "/alpha/one.txt"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["data"]["type"], "file");
    assert_eq!(v["data"]["size"], 5);

    // list root
    let out = bin()
        .args(["list", "--index"])
        .arg(&index_path)
        .args(["--path", "/"])
        .output()
        .unwrap();
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["data"]["entries"].as_array().unwrap().len(), 3);

    // read blob inline
    let out = bin()
        .args(["read", "--index"])
        .arg(&index_path)
        .args(["--path", "/alpha/two.txt"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["data"]["content_b64"], "d29ybGQ=");

    // missing path -> status error, exit 1
    let out = bin()
        .args(["lookup", "--index"])
        .arg(&index_path)
        .args(["--path", "/nope"])
        .output()
        .unwrap();
    assert!(!out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["status"], "error");
    assert_eq!(v["code"], "NOT_FOUND");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn json_stdin_mode_dispatch() {
    let dir = unique_dir("stdin");
    let index_path = dir.join("out.ifix");
    let request = json!({
        "command": "build",
        "output": index_path.to_string_lossy(),
        "root": sample_tree()
    });
    run_json(&request).expect("build via stdin");

    let lookup = json!({
        "command": "lookup",
        "index": index_path.to_string_lossy(),
        "path": "/empty.txt",
        "backend": "mmap"
    });
    let v = run_json(&lookup).expect("lookup via stdin");
    assert_eq!(v["data"]["size"], 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn verify_rejects_a_truncated_file_with_exit_1() {
    let dir = unique_dir("trunc");
    let index_path = dir.join("out.ifix");
    let request = json!({
        "command": "build",
        "output": index_path.to_string_lossy(),
        "root": sample_tree()
    });
    run_json(&request).expect("build before truncation");

    let mut bytes = std::fs::read(&index_path).unwrap();
    bytes.truncate(bytes.len() - 50);
    std::fs::write(&index_path, bytes).unwrap();

    let out = bin()
        .args(["verify", "--index"])
        .arg(&index_path)
        .output()
        .unwrap();
    assert!(!out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["status"], "error");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn numeric_flag_is_parsed_as_a_number() {
    // A 5-byte blob (one.txt) with --max-inline-base64 4 must be refused.
    // This guards that hyphenated numeric CLI flags reach the control layer
    // as JSON numbers, not strings.
    let dir = unique_dir("numflag");
    let index_path = dir.join("out.ifix");
    let request = json!({
        "command": "build",
        "output": index_path.to_string_lossy(),
        "root": sample_tree()
    });
    run_json(&request).unwrap();

    let out = bin()
        .args(["read", "--index"])
        .arg(&index_path)
        .args(["--path", "/alpha/one.txt", "--max-inline-base64", "4"])
        .output()
        .unwrap();
    assert!(!out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["code"], "LIMIT_OUTPUT");

    std::fs::remove_dir_all(&dir).ok();
}

// Feed one JSON request to `ifix json` on stdin and parse the JSON response.
fn run_json(request: &serde_json::Value) -> serde_json::Result<serde_json::Value> {
    use std::io::Write;
    use std::process::Stdio;
    let mut child = bin()
        .arg("json")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn ifix");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(&serde_json::to_vec(request).unwrap())
        .unwrap();
    let output = child.wait_with_output().expect("wait ifix");
    assert!(
        output.status.success(),
        "ifix json failed: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    serde_json::from_slice(&output.stdout)
}
