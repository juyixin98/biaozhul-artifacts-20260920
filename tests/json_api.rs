//! End-to-end tests of the JSON control entry through the compiled binary.

use std::io::Write;
use std::process::{Command, Stdio};

fn run_cli(request: &str) -> (serde_json::Value, bool) {
    let exe = env!("CARGO_BIN_EXE_bpb");
    let mut child = Command::new(exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn bpb");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(request.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    let body = String::from_utf8(out.stdout).unwrap();
    (serde_json::from_str(&body).unwrap(), out.status.success())
}

#[test]
fn encode_decode_roundtrip_via_json() {
    let values: Vec<i64> = vec![0, -1, 1, i64::MIN, i64::MAX, 12345, -67890];
    let req = serde_json::json!({"op": "encode", "values": values, "block_size": 4});
    let (resp, ok) = run_cli(&req.to_string());
    assert!(ok);
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["stats"]["input_values"], 7);
    assert_eq!(resp["stats"]["blocks"], 2);

    let data = resp["data"].as_str().unwrap();
    let req = serde_json::json!({"op": "decode", "data": data});
    let (resp, ok) = run_cli(&req.to_string());
    assert!(ok);
    let decoded: Vec<i64> = serde_json::from_value(resp["values"].clone()).unwrap();
    assert_eq!(decoded, values);
}

#[test]
fn decode_single_block_via_json() {
    let values: Vec<i64> = (0..10).map(|i| i * 7 - 30).collect();
    let req = serde_json::json!({"op": "encode", "values": values, "block_size": 5});
    let (resp, _) = run_cli(&req.to_string());
    let data = resp["data"].as_str().unwrap();

    let req = serde_json::json!({"op": "decode", "data": data, "block_index": 1});
    let (resp, ok) = run_cli(&req.to_string());
    assert!(ok);
    let decoded: Vec<i64> = serde_json::from_value(resp["values"].clone()).unwrap();
    assert_eq!(decoded, &values[5..10]);
}

#[test]
fn inspect_via_json() {
    let req = serde_json::json!({"op": "encode", "values": [5, 5, 5, 5]});
    let (resp, _) = run_cli(&req.to_string());
    let data = resp["data"].as_str().unwrap();

    let req = serde_json::json!({"op": "inspect", "data": data});
    let (resp, ok) = run_cli(&req.to_string());
    assert!(ok);
    assert_eq!(resp["header"]["total_values"], 4);
    assert_eq!(resp["blocks"][0]["bit_width"], 0);
    assert_eq!(resp["index"][0]["offset"], 28);
}

#[test]
fn max_output_limit_via_json() {
    let req = serde_json::json!({"op": "encode", "values": [1, 2, 3, 4, 5]});
    let (resp, _) = run_cli(&req.to_string());
    let data = resp["data"].as_str().unwrap();

    let req = serde_json::json!({"op": "decode", "data": data, "max_output": 2});
    let (resp, ok) = run_cli(&req.to_string());
    assert!(!ok);
    assert_eq!(resp["ok"], false);
    assert!(resp["error"].as_str().unwrap().contains("limit"));
}

#[test]
fn invalid_json_request() {
    let (resp, ok) = run_cli("this is not json");
    assert!(!ok);
    assert_eq!(resp["ok"], false);
}

#[test]
fn unknown_operation() {
    let (resp, ok) = run_cli(r#"{"op":"explode"}"#);
    assert!(!ok);
    assert_eq!(resp["ok"], false);
}

#[test]
fn bad_base64_rejected() {
    let (resp, ok) = run_cli(r#"{"op":"decode","data":"!!!not-base64!!!"}"#);
    assert!(!ok);
    assert_eq!(resp["ok"], false);
}

#[test]
fn corrupt_blob_rejected_via_json() {
    // Valid base64 of garbage bytes must fail cleanly.
    let (resp, ok) = run_cli(r#"{"op":"decode","data":"AAAAAAAABAgAAAAAAAAAAAAAAA=="}"#);
    assert!(!ok);
    assert_eq!(resp["ok"], false);
}
