//! End-to-end tests of the JSON control entry, using real files in a
//! per-test temporary directory.

use ecstripe::control;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::PathBuf;

struct TempDir(PathBuf);

impl TempDir {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "ecstripe-test-{}-{}-{}",
            name,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }

    fn path(&self, file: &str) -> String {
        self.0.join(file).to_string_lossy().into_owned()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run_json(req: &Value) -> Value {
    let text = control::run_json(&req.to_string());
    serde_json::from_str(&text).unwrap()
}

fn test_bytes(len: usize) -> Vec<u8> {
    let mut x = 0x12345678u32;
    (0..len)
        .map(|i| {
            x = x.wrapping_mul(1664525).wrapping_add(1013904223);
            (x >> 16) as u8 ^ (i % 251) as u8
        })
        .collect()
}

fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn json_encode_decode_roundtrip() {
    let tmp = TempDir::new("roundtrip");
    let data = test_bytes(10_000);
    fs::write(tmp.path("in.bin"), &data).unwrap();

    let enc = run_json(&serde_json::json!({
        "op": "encode",
        "data_shards": 4,
        "parity_shards": 2,
        "stripe_size": 512,
        "input_path": tmp.path("in.bin"),
        "output_path": tmp.path("out.ecsr"),
    }));
    assert_eq!(enc["ok"], true, "{enc}");
    assert_eq!(enc["input_bytes"], 10_000);
    assert_eq!(enc["stripes"], 5); // ceil(10000 / (4*512))

    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back.bin"),
        "missing_shards": [1, 5],
        "expected_sha256": sha256_hex(&data),
    }));
    assert_eq!(dec["ok"], true, "{dec}");
    assert_eq!(dec["verified"], true);
    assert_eq!(dec["reconstructed_shards"], 5); // shard 1 missing in each of 5 stripes
    assert_eq!(fs::read(tmp.path("back.bin")).unwrap(), data);
}

#[test]
fn json_decode_reports_sha256_without_expected() {
    let tmp = TempDir::new("hashonly");
    let data = test_bytes(700);
    fs::write(tmp.path("in.bin"), &data).unwrap();
    run_json(&serde_json::json!({
        "op": "encode", "data_shards": 2, "parity_shards": 1, "stripe_size": 128,
        "input_path": tmp.path("in.bin"), "output_path": tmp.path("out.ecsr"),
    }));

    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back.bin"),
    }));
    assert_eq!(dec["ok"], true, "{dec}");
    assert_eq!(dec["verified"], Value::Null);
    assert_eq!(dec["sha256"], sha256_hex(&data));
}

#[test]
fn json_silent_corruption_fails_external_checksum() {
    // Acceptance criterion: silent corruption is only caught by the
    // EXTERNAL checksum, never by the format itself.
    let tmp = TempDir::new("corrupt");
    let data = test_bytes(2000);
    fs::write(tmp.path("in.bin"), &data).unwrap();
    run_json(&serde_json::json!({
        "op": "encode", "data_shards": 4, "parity_shards": 2, "stripe_size": 256,
        "input_path": tmp.path("in.bin"), "output_path": tmp.path("out.ecsr"),
    }));

    // Flip a byte inside the first data block of the container.
    let mut container = fs::read(tmp.path("out.ecsr")).unwrap();
    container[20 + 4 + 3] ^= 0x40;
    fs::write(tmp.path("out.ecsr"), &container).unwrap();

    // Without expected_sha256: decode "succeeds" with wrong bytes.
    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back.bin"),
    }));
    assert_eq!(dec["ok"], true, "{dec}");
    assert_ne!(fs::read(tmp.path("back.bin")).unwrap(), data);

    // With expected_sha256: the same decode is rejected.
    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back2.bin"),
        "expected_sha256": sha256_hex(&data),
    }));
    assert_eq!(dec["ok"], false, "{dec}");
    assert!(
        dec["error"].as_str().unwrap().contains("sha256 mismatch"),
        "{dec}"
    );
}

#[test]
fn json_too_many_missing_and_output_cap() {
    let tmp = TempDir::new("limits");
    let data = test_bytes(1000);
    fs::write(tmp.path("in.bin"), &data).unwrap();
    run_json(&serde_json::json!({
        "op": "encode", "data_shards": 3, "parity_shards": 2, "stripe_size": 128,
        "input_path": tmp.path("in.bin"), "output_path": tmp.path("out.ecsr"),
    }));

    // 3 missing > m=2
    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back.bin"),
        "missing_shards": [0, 1, 4],
    }));
    assert_eq!(dec["ok"], false, "{dec}");
    assert!(dec["error"].as_str().unwrap().contains("too many missing"), "{dec}");

    // Output cap below original_len
    let dec = run_json(&serde_json::json!({
        "op": "decode",
        "input_path": tmp.path("out.ecsr"),
        "output_path": tmp.path("back.bin"),
        "max_output_bytes": 500,
    }));
    assert_eq!(dec["ok"], false, "{dec}");
    assert!(dec["error"].as_str().unwrap().contains("exceeds limit"), "{dec}");
}

#[test]
fn json_bad_requests_are_reported_not_panics() {
    // Malformed JSON
    let resp: Value = serde_json::from_str(&control::run_json("{not json")).unwrap();
    assert_eq!(resp["ok"], false);

    // Unknown op
    let resp = run_json(&serde_json::json!({"op": "explode"}));
    assert_eq!(resp["ok"], false);

    // Missing required field
    let resp = run_json(&serde_json::json!({
        "op": "encode", "data_shards": 4, "parity_shards": 2, "stripe_size": 64
    }));
    assert_eq!(resp["ok"], false);

    // Nonexistent input file
    let tmp = TempDir::new("missing");
    let resp = run_json(&serde_json::json!({
        "op": "encode", "data_shards": 4, "parity_shards": 2, "stripe_size": 64,
        "input_path": tmp.path("nope.bin"), "output_path": tmp.path("out.ecsr"),
    }));
    assert_eq!(resp["ok"], false);

    // Invalid parameters (k+m > 255)
    let resp = run_json(&serde_json::json!({
        "op": "encode", "data_shards": 250, "parity_shards": 250, "stripe_size": 64,
        "input_path": "x", "output_path": "y",
    }));
    assert_eq!(resp["ok"], false);
    assert!(resp["error"].as_str().unwrap().contains("<= 255"), "{resp}");
}
