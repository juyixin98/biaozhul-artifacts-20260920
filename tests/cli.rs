//! End-to-end tests of the JSON control entry (`bitpack-ctl`).

use std::io::Write;
use std::process::{Command, Stdio};

use serde_json::{json, Value};

fn run_ctl(request: &Value) -> (Value, bool) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_bitpack-ctl"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn bitpack-ctl");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(request.to_string().as_bytes())
        .expect("write request");
    let output = child.wait_with_output().expect("wait for bitpack-ctl");
    let response: Value = serde_json::from_slice(&output.stdout).expect("response is JSON");
    (response, output.status.success())
}

#[test]
fn encode_decode_roundtrip_via_json() {
    let values: Vec<i64> = (0..300).map(|i| i * i - 1000).collect();
    let (enc, ok) = run_ctl(&json!({"op": "encode", "values": values, "block_size": 32}));
    assert!(ok && enc["ok"] == true);
    assert_eq!(enc["stats"]["total_values"], 300);
    assert_eq!(enc["stats"]["block_count"], 10);

    let (dec, ok) = run_ctl(&json!({"op": "decode", "data": enc["data"]}));
    assert!(ok && dec["ok"] == true);
    assert_eq!(dec["values"], json!(values));
}

#[test]
fn extremes_roundtrip_via_json() {
    let values = json!([0, -1, 1, i64::MIN, i64::MAX, i64::MIN + 1, i64::MAX - 1, -i64::MAX]);
    let (enc, _) = run_ctl(&json!({"op": "encode", "values": values, "block_size": 4}));
    let (dec, ok) = run_ctl(&json!({"op": "decode", "data": enc["data"]}));
    assert!(ok);
    assert_eq!(dec["values"], values);
}

#[test]
fn inspect_and_single_block_via_json() {
    let values: Vec<i64> = (0..50).collect();
    let (enc, _) = run_ctl(&json!({"op": "encode", "values": values, "block_size": 10}));
    let (info, ok) = run_ctl(&json!({"op": "inspect", "data": enc["data"]}));
    assert!(ok);
    assert_eq!(info["header"]["block_count"], 5);
    assert_eq!(info["index"][3]["first_value_index"], 30);

    let (blk, ok) = run_ctl(&json!({"op": "decode_block", "data": enc["data"], "block": 3}));
    assert!(ok);
    assert_eq!(blk["values"], json!((30..40).collect::<Vec<i64>>()));
}

#[test]
fn bad_requests_and_bad_data_report_errors() {
    let (resp, ok) = run_ctl(&json!({"op": "nonsense"}));
    assert!(!ok && resp["ok"] == false);
    assert!(resp["error"].as_str().unwrap().contains("unknown op"));

    let (resp, ok) = run_ctl(&json!({"op": "decode", "data": "!!!not-base64!!!"}));
    assert!(!ok && resp["ok"] == false);

    // Valid base64 of garbage bytes must not crash the process.
    let (resp, ok) = run_ctl(&json!({"op": "decode", "data": "AAAAAAA="}));
    assert!(!ok && resp["ok"] == false);
}

#[test]
fn decode_limits_are_enforced_via_json() {
    let values: Vec<i64> = (0..500).collect();
    let (enc, _) = run_ctl(&json!({"op": "encode", "values": values}));
    let (resp, ok) = run_ctl(&json!({
        "op": "decode",
        "data": enc["data"],
        "limits": {"max_output_values": 100}
    }));
    assert!(!ok && resp["ok"] == false);
    assert!(resp["error"].as_str().unwrap().contains("limit exceeded"));
}
