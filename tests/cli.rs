//! End-to-end tests for the `utf8ctl` JSON control entry point.
//!
//! Each test spawns the real binary, speaks newline-delimited JSON over
//! stdin/stdout, and asserts on the parsed JSON responses.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};

use inc_utf8::json::{parse, Json};

struct Session {
    child: Child,
    stdin: ChildStdin,
    lines: std::io::Lines<BufReader<std::process::ChildStdout>>,
}

impl Session {
    fn spawn() -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_utf8ctl"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("failed to spawn utf8ctl");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        Session {
            child,
            stdin,
            lines: BufReader::new(stdout).lines(),
        }
    }

    fn request(&mut self, req: &str) -> Json {
        writeln!(self.stdin, "{req}").unwrap();
        self.stdin.flush().unwrap();
        let line = self
            .lines
            .next()
            .expect("utf8ctl closed stdout unexpectedly")
            .expect("failed to read response line");
        parse(&line).unwrap_or_else(|e| panic!("response is not valid JSON: {e}\nline: {line}"))
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn get<'a>(json: &'a Json, key: &str) -> &'a Json {
    json.get(key)
        .unwrap_or_else(|| panic!("missing key \"{key}\" in {json:?}"))
}

#[test]
fn decode_single_chunk() {
    let mut s = Session::spawn();
    // "中文" = E4B8AD E69687
    let r = s.request(r#"{"id":1,"op":"decode","data_hex":"e4b8ade69687"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    assert_eq!(
        get(&r, "codepoints"),
        &Json::Arr(vec![Json::num(0x4E2D), Json::num(0x6587)])
    );
    assert_eq!(get(&r, "output_hex"), &Json::Str("e4b8ade69687".into()));
    let r = s.request(r#"{"id":2,"op":"finish"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
}

#[test]
fn decode_split_across_chunks() {
    let mut s = Session::spawn();
    // 🦀 = F0 9F A6 80, split 2+2; must emit only after the 4th byte.
    let r = s.request(r#"{"id":1,"op":"decode","data_hex":"f09f"}"#);
    assert_eq!(get(&r, "codepoints"), &Json::Arr(vec![]));
    let r = s.request(r#"{"id":2,"op":"decode","data_hex":"a680"}"#);
    assert_eq!(get(&r, "codepoints"), &Json::Arr(vec![Json::num(0x1F980)]));
    let r = s.request(r#"{"id":3,"op":"stats"}"#);
    assert_eq!(get(&r, "consumed"), &Json::num(4));
    assert_eq!(get(&r, "emitted"), &Json::num(1));
}

#[test]
fn truncated_sequence_reports_offset() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"decode","data_hex":"616263e4b8"}"#); // "abc" + half of 中
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    let r = s.request(r#"{"id":2,"op":"finish"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(false));
    let errors = get(&r, "errors");
    let first = &errors.as_array().unwrap()[0];
    assert_eq!(get(first, "kind"), &Json::Str("truncated_sequence".into()));
    assert_eq!(get(first, "offset"), &Json::num(3));
    assert_eq!(get(first, "sequence_start"), &Json::num(3));
}

#[test]
fn invalid_bytes_fail_fast() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"decode","data_hex":"61ff62"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(false));
    assert_eq!(get(&r, "stopped"), &Json::Bool(true));
    let errors = get(&r, "errors");
    let first = &errors.as_array().unwrap()[0];
    assert_eq!(get(first, "kind"), &Json::Str("invalid_lead_byte".into()));
    assert_eq!(get(first, "offset"), &Json::num(1));
}

#[test]
fn recovery_skip_continues_after_error() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"configure","recovery":"skip"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    let r = s.request(r#"{"id":2,"op":"decode","data_hex":"61ff62"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(false)); // errors happened
    assert_eq!(get(&r, "stopped"), &Json::Bool(false)); // but decoding continued
    assert_eq!(
        get(&r, "codepoints"),
        &Json::Arr(vec![Json::num(0x61), Json::num(0x62)])
    );
}

#[test]
fn encode_roundtrip_via_cli() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"encode","codepoints":[97,20013,128029]}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    let hex = get(&r, "data_hex").as_str().unwrap().to_string();
    assert_eq!(hex, "61e4b8adf09f909d");
    let r = s.request(&format!(r#"{{"id":2,"op":"decode","data_hex":"{hex}"}}"#));
    assert_eq!(
        get(&r, "codepoints"),
        &Json::Arr(vec![Json::num(97), Json::num(20013), Json::num(128029)])
    );
}

#[test]
fn encode_rejects_surrogate() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"encode","codepoints":[55296]}"#); // U+D800
    assert_eq!(get(&r, "ok"), &Json::Bool(false));
    assert!(get(&r, "error").as_str().unwrap().contains("surrogate"));
}

#[test]
fn limits_via_configure() {
    let mut s = Session::spawn();
    let r = s.request(r#"{"id":1,"op":"configure","limits":{"max_input_bytes":2}}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    let r = s.request(r#"{"id":2,"op":"decode","data_hex":"61616161"}"#);
    assert_eq!(get(&r, "stopped"), &Json::Bool(true));
    let errors = get(&r, "errors");
    assert_eq!(
        get(&errors.as_array().unwrap()[0], "kind"),
        &Json::Str("input_limit_exceeded".into())
    );
}

#[test]
fn malformed_request_gets_error_response() {
    let mut s = Session::spawn();
    let r = s.request("this is not json");
    assert_eq!(get(&r, "ok"), &Json::Bool(false));
    let r = s.request(r#"{"id":7,"op":"nonsense"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(false));
    assert_eq!(get(&r, "id"), &Json::num(7));
}

#[test]
fn reset_starts_fresh() {
    let mut s = Session::spawn();
    let _ = s.request(r#"{"id":1,"op":"decode","data_hex":"e4b8"}"#);
    let r = s.request(r#"{"id":2,"op":"reset"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
    // After reset the dangling half-sequence is gone.
    let r = s.request(r#"{"id":3,"op":"finish"}"#);
    assert_eq!(get(&r, "ok"), &Json::Bool(true));
}
