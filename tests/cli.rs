//! CLI（JSON 控制入口）端到端测试。

use std::io::Write;
use std::process::{Command, Stdio};

use lzsw::json::{self, Json};

fn run_cli(request: &str) -> (Json, i32) {
    let exe = env!("CARGO_BIN_EXE_lzsw");
    let mut child = Command::new(exe)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn lzsw");
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(request.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    let stdout = String::from_utf8(out.stdout).unwrap();
    let code = out.status.code().unwrap_or(-1);
    let json = json::parse(stdout.trim()).unwrap_or_else(|e| {
        panic!("response is not valid JSON: {e}\nstdout: {stdout}");
    });
    (json, code)
}

#[test]
fn cli_roundtrip() {
    let payload = lzsw::base64::encode(b"hello hello hello hello");
    let req = format!(r#"{{"op":"compress","input_base64":"{payload}","chunk":5}}"#);
    let (resp, code) = run_cli(&req);
    assert_eq!(code, 0);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(true)));
    let compressed = resp
        .get("output_base64")
        .and_then(Json::as_str)
        .unwrap()
        .to_string();

    let req2 = format!(r#"{{"op":"decompress","input_base64":"{compressed}","chunk":2}}"#);
    let (resp2, code2) = run_cli(&req2);
    assert_eq!(code2, 0);
    let decoded =
        lzsw::base64::decode(resp2.get("output_base64").and_then(Json::as_str).unwrap()).unwrap();
    assert_eq!(decoded, b"hello hello hello hello");
}

#[test]
fn cli_reports_bomb_error() {
    let bomb = lzsw::compress(&vec![b'a'; 1_000_000], 4096).unwrap();
    let req = format!(
        r#"{{"op":"decompress","input_base64":"{}","max_output":100}}"#,
        lzsw::base64::encode(&bomb)
    );
    let (resp, code) = run_cli(&req);
    assert_eq!(code, 1);
    assert_eq!(resp.get("ok"), Some(&Json::Bool(false)));
    assert_eq!(
        resp.get("error_kind").and_then(Json::as_str),
        Some("output_limit_exceeded")
    );
}

#[test]
fn cli_rejects_bad_request() {
    let (resp, code) = run_cli("not json");
    assert_eq!(code, 1);
    assert_eq!(
        resp.get("error_kind").and_then(Json::as_str),
        Some("json_error")
    );

    let (resp2, code2) = run_cli(r#"{"op":"explode"}"#);
    assert_eq!(code2, 1);
    assert_eq!(
        resp2.get("error_kind").and_then(Json::as_str),
        Some("unknown_op")
    );
}
