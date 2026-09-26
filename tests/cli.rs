//! End-to-end tests for the JSON control entry point: spawn the built
//! binary, feed requests on stdin, assert on the JSON responses and
//! exit codes.

use std::io::Write;
use std::process::{Command, Stdio};

use incutf8::json::{self, Json};

fn run_cli(request: &str) -> (i32, Json) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_incutf8"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn incutf8");
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(request.as_bytes())
        .expect("write request");
    let out = child.wait_with_output().expect("wait for incutf8");
    let stdout = String::from_utf8(out.stdout).expect("response is UTF-8");
    let code = out.status.code().unwrap_or(-1);
    let response = json::parse(stdout.trim()).unwrap_or_else(|e| {
        panic!("response is not JSON ({}): {}", e, stdout)
    });
    (code, response)
}

fn b64(data: &[u8]) -> String {
    incutf8::base64::encode(data)
}

#[test]
fn ping() {
    let (code, resp) = run_cli(r#"{"op":"ping"}"#);
    assert_eq!(code, 0);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
}

#[test]
fn decode_simple() {
    let req = format!(r#"{{"op":"decode","chunks":["{}"]}}"#, b64("hello, 世界".as_bytes()));
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 0, "response: {:?}", resp);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    assert_eq!(
        resp.get("output_text").and_then(Json::as_str),
        Some("hello, 世界")
    );
    assert_eq!(resp.get("output_codepoints").and_then(Json::as_u64), Some(9));
    assert_eq!(resp.get("consumed_bytes").and_then(Json::as_u64), Some(13));
}

#[test]
fn decode_multibyte_split_across_chunks() {
    // 🦀 = F0 9F A6 80, split after the second byte.
    let req = format!(
        r#"{{"op":"decode","chunks":["{}","{}"],"final":true}}"#,
        b64(&[0xF0, 0x9F]),
        b64(&[0xA6, 0x80])
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 0, "response: {:?}", resp);
    assert_eq!(resp.get("output_text").and_then(Json::as_str), Some("🦀"));
}

#[test]
fn decode_invalid_byte_reports_offset() {
    let req = format!(
        r#"{{"op":"decode","chunks":["{}"]}}"#,
        b64(&[0x61, 0x62, 0xFF, 0x63])
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1, "decode errors must exit 1");
    assert_eq!(resp.get("ok"), Some(&Json::Bool(false)));
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(errors.len(), 1);
    assert_eq!(
        errors[0].get("kind").and_then(Json::as_str),
        Some("invalid_lead_byte")
    );
    assert_eq!(errors[0].get("offset").and_then(Json::as_u64), Some(2));
    assert_eq!(
        errors[0].get("byte_hex").and_then(Json::as_str),
        Some("0xFF")
    );
}

#[test]
fn decode_incomplete_at_final_reports_lead_offset() {
    // "ab" + E2 82 (unfinished 3-byte sequence).
    let req = format!(
        r#"{{"op":"decode","chunks":["{}"],"final":true}}"#,
        b64(&[0x61, 0x62, 0xE2, 0x82])
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1);
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(
        errors[0].get("kind").and_then(Json::as_str),
        Some("incomplete_sequence")
    );
    assert_eq!(errors[0].get("offset").and_then(Json::as_u64), Some(2));
    assert_eq!(resp.get("pending_sequence_bytes").and_then(Json::as_u64), Some(2));
}

#[test]
fn decode_non_final_pending_sequence_is_not_an_error() {
    let req = format!(
        r#"{{"op":"decode","chunks":["{}"],"final":false}}"#,
        b64(&[0xE2, 0x82])
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 0, "response: {:?}", resp);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    assert_eq!(resp.get("pending_sequence_bytes").and_then(Json::as_u64), Some(2));
}

#[test]
fn decode_overlong_rejected() {
    let req = format!(r#"{{"op":"decode","data":"{}"}}"#, b64(&[0xC0, 0x80]));
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1);
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(
        errors[0].get("kind").and_then(Json::as_str),
        Some("overlong_encoding")
    );
}

#[test]
fn decode_surrogate_rejected() {
    let req = format!(r#"{{"op":"decode","data":"{}"}}"#, b64(&[0xED, 0xA0, 0x80]));
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1);
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(
        errors[0].get("kind").and_then(Json::as_str),
        Some("surrogate_code_point")
    );
}

#[test]
fn decode_collect_policy_recovers() {
    let req = format!(
        r#"{{"op":"decode","data":"{}","error_policy":"collect"}}"#,
        b64(b"A\xFFB")
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1);
    assert_eq!(resp.get("output_text").and_then(Json::as_str), Some("AB"));
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(errors.len(), 1);
}

#[test]
fn validate_op_produces_no_output() {
    let req = format!(r#"{{"op":"validate","data":"{}"}}"#, b64("valid 中".as_bytes()));
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 0);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    assert!(resp.get("output_text").is_none());
}

#[test]
fn encode_text_roundtrips_through_decode() {
    let req = r#"{"op":"encode","text":"héllo 🦀 中"}"#;
    let (code, resp) = run_cli(req);
    assert_eq!(code, 0);
    let data_b64 = resp.get("data_base64").and_then(Json::as_str).unwrap();
    let bytes = incutf8::base64::decode(data_b64).unwrap();
    assert_eq!(bytes, "héllo 🦀 中".as_bytes());
}

#[test]
fn encode_codepoints_rejects_surrogate() {
    let req = r#"{"op":"encode","codepoints":[65, 55296, 66]}"#; // 55296 = U+D800
    let (code, resp) = run_cli(req);
    assert_eq!(code, 1);
    let errors = resp.get("errors").and_then(Json::as_arr).unwrap();
    assert_eq!(errors.len(), 1);
    assert_eq!(
        errors[0].get("kind").and_then(Json::as_str),
        Some("surrogate_code_point")
    );
    assert_eq!(errors[0].get("index").and_then(Json::as_u64), Some(1));
}

#[test]
fn malformed_request_exits_2() {
    let (code, resp) = run_cli("this is not json");
    assert_eq!(code, 2);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(false)));
}

#[test]
fn unknown_op_exits_2() {
    let (code, _resp) = run_cli(r#"{"op":"explode"}"#);
    assert_eq!(code, 2);
}

#[test]
fn output_limit_via_request() {
    let req = format!(
        r#"{{"op":"decode","data":"{}","max_output_codepoints":2}}"#,
        b64(b"abcd")
    );
    let (code, resp) = run_cli(&req);
    assert_eq!(code, 1);
    assert_eq!(resp.get("truncated"), Some(&Json::Bool(true)));
    assert_eq!(resp.get("output_text").and_then(Json::as_str), Some("ab"));
}
