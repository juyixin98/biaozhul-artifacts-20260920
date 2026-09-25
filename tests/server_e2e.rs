//! End-to-end tests against the real TCP server binary over loopback.
//!
//! The server binary is spawned by Cargo (CARGO_BIN_EXE_*), binds port 0,
//! and tests talk to it with raw sockets so every byte — including ambiguous
//! line endings — is exactly what the test decides.

mod common;

use common::Conn;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command};
use std::time::Duration;

struct ServerGuard {
    child: Child,
    addr: String,
}

impl ServerGuard {
    fn start(args: &[&str]) -> ServerGuard {
        let bin = env!("CARGO_BIN_EXE_http-framing-server");
        let mut cmd = Command::new(bin);
        cmd.args(["--bind", "127.0.0.1:0", "--idle-timeout", "3"]);
        cmd.args(args);
        cmd.stderr(std::process::Stdio::piped());
        cmd.stdout(std::process::Stdio::null());
        let mut child = cmd.spawn().expect("start server binary");
        let stderr = child.stderr.take().expect("piped stderr");

        // Parse "listening on 127.0.0.1:NNNNN".
        let addr = (|| {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                if let Some(rest) = line.strip_prefix("listening on ") {
                    return rest.trim().to_string();
                }
            }
            panic!("server never printed listening address");
        })();

        // Give the kernel a moment (address is already bound by the time the
        // banner is printed, but be defensive on slow CI).
        std::thread::sleep(Duration::from_millis(20));
        ServerGuard { child, addr }
    }
}

impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn get_returns_json_200() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str("GET /hello HTTP/1.1\r\nHost: e\r\n\r\n")
        .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 200);
    let body = String::from_utf8_lossy(&r.body);
    assert!(body.contains("\"method\":\"GET\""), "{body}");
    assert!(body.contains("\"target\":\"/hello\""));
    assert!(body.contains("\"framing\":\"none\""));
}

#[test]
fn fixed_length_post_echoes_body_len() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str("POST /up HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello")
        .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 200);
    let body = String::from_utf8_lossy(&r.body);
    assert!(body.contains("\"framing\":\"fixed-length\""), "{body}");
    assert!(body.contains("\"content_length\":5"));
    assert!(body.contains("\"body\":\"hello\""));
}

#[test]
fn chunked_post_reports_framing() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str(
        "POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
         5\r\nhello\r\n0\r\nX-T: 1\r\n\r\n",
    )
    .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 200);
    let body = String::from_utf8_lossy(&r.body);
    assert!(body.contains("\"framing\":\"chunked\""), "{body}");
    assert!(body.contains("\"body\":\"hello\""));
    assert!(body.contains("\"trailer_count\":1"));
}

#[test]
fn pipelined_requests_each_get_a_response_in_order() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    // Send both requests in ONE write call — the server must pipeline.
    c.send_str("GET /a HTTP/1.1\r\nHost: e\r\n\r\nGET /b HTTP/1.1\r\nHost: e\r\n\r\n")
        .unwrap();
    let r1 = c.read_one().unwrap();
    let r2 = c.read_one().unwrap();
    assert_eq!(r1.status, 200);
    assert_eq!(r2.status, 200);
    let b1 = String::from_utf8_lossy(&r1.body);
    let b2 = String::from_utf8_lossy(&r2.body);
    assert!(b1.contains("\"target\":\"/a\""));
    assert!(b2.contains("\"target\":\"/b\""));
    assert!(b1.contains("\"seq\":1"));
    assert!(b2.contains("\"seq\":2"));
}

#[test]
fn segmented_delivery_is_handled() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    let msg = b"POST /up HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world";
    // Deliver one byte at a time with small pacing — server must wait.
    for b in msg {
        c.send(&[*b]).unwrap();
        std::thread::sleep(Duration::from_micros(300));
    }
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 200);
    assert!(String::from_utf8_lossy(&r.body).contains("\"body\":\"hello world\""));
}

#[test]
fn smuggling_cl_te_is_400_and_connection_closes() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str(
        "POST / HTTP/1.1\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    )
    .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 400);
    let body = String::from_utf8_lossy(&r.body);
    assert!(body.contains("\"error\":\"te_and_cl\""), "{body}");
    // Connection header must announce the close.
    assert!(r
        .headers
        .iter()
        .any(|(k, v)| k.eq_ignore_ascii_case("connection") && v.eq_ignore_ascii_case("close")));
    // Any subsequent pipelined bytes must be discarded: server closes.
    let rest = c.expect_closed().unwrap();
    // There should be no second 200 response hiding in the tail.
    assert!(
        !rest.windows(12).any(|w| w == b"HTTP/1.1 200"),
        "second response after fatal framing error: {rest:?}"
    );
}

#[test]
fn bare_lf_is_400() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send(b"GET / HTTP/1.1\nHost: e\r\n\r\n").unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 400);
    assert!(String::from_utf8_lossy(&r.body).contains("bare_line_feed"));
}

#[test]
fn oversized_body_is_413() {
    let srv = ServerGuard::start(&["--max-body", "10"]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str("POST /up HTTP/1.1\r\nContent-Length: 11\r\n\r\n0123456789A")
        .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 413);
    assert!(String::from_utf8_lossy(&r.body).contains("body_too_large"));
}

#[test]
fn oversized_header_section_is_400() {
    let srv = ServerGuard::start(&["--max-header-section", "100"]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    let mut req = b"GET / HTTP/1.1\r\nX: ".to_vec();
    req.extend_from_slice(&[b'a'; 500]);
    req.extend_from_slice(b"\r\n\r\n");
    c.send(&req).unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 400);
    assert!(String::from_utf8_lossy(&r.body).contains("header_section_too_large"));
}

#[test]
fn connection_close_request_ends_connection() {
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str("GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
        .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 200);
    let data = c.expect_closed().unwrap();
    assert!(!data.windows(12).any(|w| w == b"HTTP/1.1 200"));
}

#[test]
fn chunked_size_mismatch_is_400() {
    // Declared chunk size 4 but 5 bytes follow before CRLF: the 5th byte
    // lands where the mandatory post-chunk CR must be — rejected, and the
    // hidden trailing request can never be framed.
    let srv = ServerGuard::start(&[]);
    let mut c = Conn::connect(&srv.addr).unwrap();
    c.send_str(
        "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
         4\r\nhello\r\n0\r\n\r\nGET /smuggled HTTP/1.1\r\n\r\n",
    )
    .unwrap();
    let r = c.read_one().unwrap();
    assert_eq!(r.status, 400);
    let data = c.expect_closed().unwrap();
    assert!(
        !data.windows(18).any(|w| w == b"GET /smuggled HTTP"),
        "smuggled request must not be echoed back"
    );
}
