//! End-to-end HTTP verification: a real server thread on an ephemeral port,
//! driven over raw TCP (no HTTP client dependency). Also exercises the
//! file-backed atomic build path and corruption reporting through HTTP.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use psst::server::HttpServer;

struct TestServer {
    addr: String,
    dir: PathBuf,
}

fn spawn_server() -> TestServer {
    let dir = std::env::temp_dir().join(format!(
        "psst-http-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let server = HttpServer::bind(dir.clone(), "127.0.0.1:0").unwrap();
    let addr = server.local_addr().unwrap();
    thread::spawn(move || {
        let _ = server.serve();
    });
    let ts = TestServer { addr, dir };
    // Wait for health.
    for _ in 0..50 {
        if ts.raw("GET", "/healthz", None).is_ok() {
            return ts;
        }
        thread::sleep(Duration::from_millis(20));
    }
    panic!("server did not come up");
}

impl Drop for TestServer {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn http(addr: &str, method: &str, path: &str, body: Option<&str>) -> (u16, String) {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n");
    if let Some(b) = body {
        req.push_str(&format!("Content-Length: {}\r\n", b.len()));
    }
    req.push_str("\r\n");
    if let Some(b) = body {
        req.push_str(b);
    }
    stream.write_all(req.as_bytes()).unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let text = String::from_utf8_lossy(&raw);
    let status: u16 = text.split_whitespace().nth(1).unwrap().parse().unwrap();
    let json = text.split_once("\r\n\r\n").map(|(_, b)| b).unwrap_or("");
    (status, json.to_string())
}

impl TestServer {
    fn raw(&self, method: &str, path: &str, body: Option<&str>) -> std::io::Result<(u16, String)> {
        Ok(http(&self.addr, method, path, body))
    }
    fn req(&self, method: &str, path: &str, body: Option<&str>) -> (u16, String) {
        self.raw(method, path, body).unwrap()
    }
}

/// Minimal substring assertions keep the tests independent of key ordering in
/// JSON objects.
fn assert_contains(haystack: &str, needle: &str) {
    assert!(
        haystack.contains(needle),
        "expected response to contain {needle:?}, got: {haystack}"
    );
}

#[test]
fn full_lifecycle_over_http() {
    let s = spawn_server();

    let (code, body) = s.req("GET", "/healthz", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"ok\":true");

    // Build a table. Keys are hex: "", 00, 61 ("a"), 62 ("b"), prefix-heavy.
    let payload = concat!(
        "{\"block_size\":256,\"restart_interval\":4,\"entries\":[",
        "[\"\",\"7630\"],[\"00\",\"7631\"],[\"61616130\",\"7632\"],[\"61616131\",\"7633\"],[\"6161613261\",\"7634\"],[\"6161613262\",\"7635\"],[\"7a\",\"7636\"]",
        "]}"
    );
    let (code, body) = s.req("PUT", "/tables/demo", Some(payload));
    assert_eq!(code, 201, "{body}");
    assert_contains(&body, "\"ok\":true");
    assert_contains(&body, "\"entries\":7");

    // Duplicate build over an existing file is rejected (create_new).
    let (code, _) = s.req("PUT", "/tables/demo", Some(payload));
    assert_eq!(code, 409);

    // Point gets.
    let (code, body) = s.req("GET", "/tables/demo/get?key=", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"found\":true");
    assert_contains(&body, "\"value\":\"7630\"");

    let (code, body) = s.req("GET", "/tables/demo/get?key=61616131", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"value\":\"7633\"");

    let (code, body) = s.req("GET", "/tables/demo/get?key=ff", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"found\":false");

    // Missing key parameter is a 400.
    let (code, _) = s.req("GET", "/tables/demo/get", None);
    assert_eq!(code, 400);

    // Range scan across multiple blocks.
    let (code, body) = s.req("GET", "/tables/demo/scan?start=61616130&end=7a", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"count\":4"); // aaa0 aaa1 aaa2a aaa2b
    assert_contains(&body, "\"61616130\"");
    assert_contains(&body, "\"6161613262\"");

    // Inclusive end + limit.
    let (code, body) = s.req(
        "GET",
        "/tables/demo/scan?end=61616131&end_inclusive&limit=2",
        None,
    );
    assert_eq!(code, 200);
    assert_contains(&body, "\"count\":2");

    // Validate.
    let (code, body) = s.req("POST", "/tables/demo/validate", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"valid\":true");
    assert_contains(&body, "\"total_entries\":7");

    // Listing includes the table.
    let (code, body) = s.req("GET", "/tables", None);
    assert_eq!(code, 200);
    assert_contains(&body, "demo");

    // File exists at the expected path.
    assert!(s.dir.join("demo").is_file());

    // Delete and re-list.
    let (code, body) = s.req("DELETE", "/tables/demo", None);
    assert_eq!(code, 200);
    assert_contains(&body, "\"deleted\":\"demo\"");
    assert!(!s.dir.join("demo").exists());

    // Getting a missing table is 404.
    let (code, _) = s.req("GET", "/tables/demo/get?key=00", None);
    assert_eq!(code, 404);
}

#[test]
fn http_reports_corruption_with_400() {
    let s = spawn_server();
    let payload = "{\"entries\":[[\"6b31\",\"7631\"],[\"6b32\",\"7632\"],[\"6b33\",\"7633\"],[\"6b34\",\"7634\"],[\"6b35\",\"7635\"]]}";
    let (code, _) = s.req("PUT", "/tables/bad", Some(payload));
    assert_eq!(code, 201);

    // Corrupt one byte of the file on disk, then validation must fail cleanly.
    let path = s.dir.join("bad");
    let mut bytes = std::fs::read(&path).unwrap();
    bytes[10] ^= 0xff;
    std::fs::write(&path, bytes).unwrap();

    let (code, body) = s.req("POST", "/tables/bad/validate", None);
    assert_eq!(code, 400);
    assert_contains(&body, "\"ok\":false");
    assert_contains(&body, "\"error_kind\":\"corruption\"");
}

#[test]
fn http_rejects_bad_requests() {
    let s = spawn_server();

    // Malformed JSON.
    let (code, _) = s.req("PUT", "/tables/x", Some("{not json"));
    assert_eq!(code, 400);

    // Missing entries.
    let (code, _) = s.req("PUT", "/tables/x", Some("{}"));
    assert_eq!(code, 400);

    // Odd-length hex key.
    let (code, body) = s.req("PUT", "/tables/x", Some("{\"entries\":[[\"abc\",\"\"]]}"));
    assert_eq!(code, 400);
    assert_contains(&body, "hex");

    // Path traversal: '..' as a real path segment changes the route shape, so
    // there is no matching route; nothing may be created anywhere.
    let before = dir_listing(&s.dir);
    let (code, _) = s.req("PUT", "/tables/../x", Some("{\"entries\":[]}"));
    assert!(code == 400 || code == 404, "got {code}");
    assert_eq!(dir_listing(&s.dir), before, "data dir changed");

    // Zero entries refused.
    let (code, _) = s.req("PUT", "/tables/y", Some("{\"entries\":[]}"));
    assert_eq!(code, 400);

    // Unknown route.
    let (code, _) = s.req("GET", "/nope", None);
    assert_eq!(code, 404);
}

fn dir_listing(p: &std::path::Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(p)
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
        .collect();
    v.sort();
    v
}
