//! End-to-end test of the JSON control entry via the compiled `chf` binary.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_chf")
}

fn tmpdir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("chf-test-{}-{}", tag, std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn run_chf(request_json: &str, dir: &std::path::Path) -> (String, i32) {
    let req_path = dir.join("request.json");
    fs::write(&req_path, request_json).unwrap();
    let out = Command::new(bin()).arg(&req_path).output().unwrap();
    let stdout = String::from_utf8(out.stdout).unwrap();
    (stdout, out.status.code().unwrap_or(-1))
}

#[test]
fn cli_compress_decompress_roundtrip() {
    let dir = tmpdir("roundtrip");
    let input = dir.join("input.bin");
    let compressed = dir.join("data.chf");
    let restored = dir.join("restored.bin");

    // Deterministic pseudo-random content, 200 KiB.
    let mut state = 0xDEAD_BEEFu64;
    let mut data = Vec::new();
    for _ in 0..(200 * 1024 / 8) {
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        data.extend_from_slice(&state.to_le_bytes());
    }
    fs::write(&input, &data).unwrap();

    let (resp, code) = run_chf(
        &format!(
            "{{\"op\":\"compress\",\"input\":\"{}\",\"output\":\"{}\"}}",
            input.display(),
            compressed.display()
        ),
        &dir,
    );
    assert_eq!(code, 0, "compress failed: {resp}");
    assert!(resp.contains("\"ok\":true"), "unexpected response: {resp}");
    assert!(resp.contains("\"ratio\":"), "ratio missing: {resp}");

    let (resp, code) = run_chf(
        &format!(
            "{{\"op\":\"decompress\",\"input\":\"{}\",\"output\":\"{}\"}}",
            compressed.display(),
            restored.display()
        ),
        &dir,
    );
    assert_eq!(code, 0, "decompress failed: {resp}");
    assert!(resp.contains("\"ok\":true"), "unexpected response: {resp}");
    assert_eq!(fs::read(&restored).unwrap(), data, "round-trip mismatch");

    fs::remove_dir_all(&dir).ok();
}

#[test]
fn cli_rejects_unknown_op() {
    let dir = tmpdir("badop");
    let (resp, code) = run_chf("{\"op\":\"explode\",\"input\":\"a\",\"output\":\"b\"}", &dir);
    assert_eq!(code, 1);
    assert!(resp.contains("\"ok\":false"), "unexpected response: {resp}");
    assert!(resp.contains("unknown op"), "unexpected response: {resp}");
    fs::remove_dir_all(&dir).ok();
}

#[test]
fn cli_rejects_malformed_json() {
    let dir = tmpdir("badjson");
    let (resp, code) = run_chf("{not json", &dir);
    assert_eq!(code, 1);
    assert!(resp.contains("\"ok\":false"), "unexpected response: {resp}");
    fs::remove_dir_all(&dir).ok();
}

#[test]
fn cli_rejects_truncated_stream_file() {
    let dir = tmpdir("trunc");
    let input = dir.join("input.bin");
    let compressed = dir.join("data.chf");
    let restored = dir.join("restored.bin");

    fs::write(&input, b"truncate me, please, truncate me").unwrap();
    let (resp, code) = run_chf(
        &format!(
            "{{\"op\":\"compress\",\"input\":\"{}\",\"output\":\"{}\"}}",
            input.display(),
            compressed.display()
        ),
        &dir,
    );
    assert_eq!(code, 0, "compress failed: {resp}");

    // Truncate the compressed file, then attempt to decode it.
    let full = fs::read(&compressed).unwrap();
    fs::write(&compressed, &full[..full.len() - 2]).unwrap();
    let (resp, code) = run_chf(
        &format!(
            "{{\"op\":\"decompress\",\"input\":\"{}\",\"output\":\"{}\"}}",
            compressed.display(),
            restored.display()
        ),
        &dir,
    );
    assert_eq!(code, 1, "expected failure, got: {resp}");
    assert!(resp.contains("truncated"), "unexpected response: {resp}");
    fs::remove_dir_all(&dir).ok();
}
