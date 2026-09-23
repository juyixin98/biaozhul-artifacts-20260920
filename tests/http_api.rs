//! End-to-end HTTP tests against a real loopback TCP server.
//!
//! Demonstrates the verifier boundary: a SECOND server instance (or any
//! process) verifies a /range response using only the JSON in that response
//! plus a trusted root — it never opens the repository under test.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use merkle_store::api::RepoHandler;
use merkle_store::http;
use merkle_store::json::{self, Json};
use merkle_store::store::Store;
use merkle_store::vfs::{MemVfs, RealVfs};

fn tempdir(tag: &str) -> PathBuf {
    let mut d = std::env::temp_dir();
    d.push(format!(
        "merkle-store-http-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

struct RawResponse {
    status: u16,
    body: String,
}

fn request(addr: &str, method: &str, target: &str, body: &[u8]) -> RawResponse {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let head = format!(
        "{method} {target} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").unwrap() + 4;
    let status = String::from_utf8_lossy(&raw[..20]);
    let status = status.split_whitespace().nth(1).unwrap().parse().unwrap();
    RawResponse {
        status,
        body: String::from_utf8_lossy(&raw[split..]).into_owned(),
    }
}

fn spawn_server<H: http::Handler + 'static>(handler: Arc<H>) -> String {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    thread::spawn(move || {
        let _ = http::serve(listener, handler);
    });
    // Wait briefly for the thread to accept.
    thread::sleep(Duration::from_millis(50));
    addr
}

fn spawn_real_store(dir: &std::path::Path) -> String {
    let store = Store::open(RealVfs::new(), dir).unwrap();
    spawn_server(RepoHandler::shared(store))
}

/// A verifier server backed by an EMPTY in-memory FS — proving it never
/// touches the target repository's files during /verify.
fn spawn_stateless_verifier() -> String {
    // RepoHandler wraps a Store; give it an unrelated empty MemVfs repo.
    let mut store = Store::create(MemVfs::new(), std::path::Path::new("/verifier"), 4).unwrap();
    store.reset(b"").unwrap();
    spawn_server(RepoHandler::shared(store))
}

#[test]
fn http_build_root_range_and_verify() {
    let dir = tempdir("flow");
    {
        Store::create(RealVfs::new(), &dir, 4).unwrap();
    }
    let addr = spawn_real_store(&dir);
    let verifier = spawn_stateless_verifier();

    // health
    let r = request(&addr, "GET", "/health", b"");
    assert_eq!(r.status, 200);
    assert!(r.body.contains("\"status\": \"ok\""));

    // build full file
    let payload = b"abcdefghijk"; // 11 bytes -> 3 blocks (4,4,3)
    let r = request(&addr, "POST", "/build", payload);
    assert_eq!(r.status, 200);
    let build = json::parse(&r.body).unwrap();
    assert_eq!(build.get("n").and_then(Json::as_i64), Some(3));
    let trusted_root = build.get("root").unwrap().as_str().unwrap().to_string();

    // root endpoint agrees
    let r = request(&addr, "GET", "/root", b"");
    let rootj = json::parse(&r.body).unwrap();
    assert_eq!(
        rootj.get("root").unwrap().as_str(),
        Some(trusted_root.as_str())
    );

    // first range
    let r = request(&addr, "GET", "/range?which=first", b"");
    assert_eq!(r.status, 200);
    let range1 = json::parse(&r.body).unwrap();
    assert_eq!(range1.get("start").and_then(Json::as_i64), Some(0));
    // The response already carries everything the verifier needs.
    assert!(!range1.get("proof").unwrap().as_array().unwrap().is_empty());

    // Verify on the independent verifier server: reuse the response JSON,
    // injecting the trusted root.
    let mut verify_body = range1.clone();
    if let json::Json::Object(ref mut m) = verify_body {
        m.insert("root".to_string(), Json::Str(trusted_root.clone()));
    }
    let text = verify_body.compact();
    let r = request(&verifier, "POST", "/verify", text.as_bytes());
    assert_eq!(r.status, 200);
    let v = json::parse(&r.body).unwrap();
    assert_eq!(
        v.get("valid").and_then(Json::as_bool),
        Some(true),
        "body: {}",
        r.body
    );

    // last range (short block) verifies too
    let r = request(&addr, "GET", "/range?which=last", b"");
    let range_last = json::parse(&r.body).unwrap();
    let mut body = range_last.clone();
    if let json::Json::Object(ref mut m) = body {
        m.insert("root".to_string(), Json::Str(trusted_root.clone()));
    }
    let r = request(&verifier, "POST", "/verify", body.compact().as_bytes());
    let v = json::parse(&r.body).unwrap();
    assert_eq!(v.get("valid").and_then(Json::as_bool), Some(true));

    // tamper one block byte -> invalid
    let mut tampered = range1.clone();
    if let json::Json::Object(ref mut m) = tampered {
        m.insert("root".to_string(), Json::Str(trusted_root.clone()));
        if let Some(Json::Array(blocks)) = m.get_mut("blocks") {
            if let Json::Object(first) = &mut blocks[0] {
                let enc = first.get("data").unwrap().as_str().unwrap();
                let mut bytes = merkle_store::base64::decode(enc).unwrap();
                bytes[0] ^= 0x01;
                first.insert(
                    "data".to_string(),
                    Json::Str(merkle_store::base64::encode(&bytes)),
                );
            }
        }
    }
    let r = request(&verifier, "POST", "/verify", tampered.compact().as_bytes());
    let v = json::parse(&r.body).unwrap();
    assert_eq!(v.get("valid").and_then(Json::as_bool), Some(false));
    assert!(v
        .get("error")
        .unwrap()
        .as_str()
        .unwrap()
        .contains("mismatch"));

    // forged length -> invalid
    let mut forged = range1.clone();
    if let json::Json::Object(ref mut m) = forged {
        m.insert("root".to_string(), Json::Str(trusted_root.clone()));
        m.insert("data_len".to_string(), Json::Int(99));
    }
    let r = request(&verifier, "POST", "/verify", forged.compact().as_bytes());
    let v = json::parse(&r.body).unwrap();
    assert_eq!(v.get("valid").and_then(Json::as_bool), Some(false));
}

#[test]
fn http_empty_file_verifies() {
    let dir = tempdir("emptyhttp");
    Store::create(RealVfs::new(), &dir, 8).unwrap();
    let addr = spawn_real_store(&dir);
    let verifier = spawn_stateless_verifier();

    let r = request(&addr, "GET", "/range?which=first", b"");
    let range = json::parse(&r.body).unwrap();
    let mut body = range;
    if let json::Json::Object(ref mut m) = body {
        // Pull the committed empty-root sentinel from /root.
        let rj = json::parse(&request(&addr, "GET", "/root", b"").body).unwrap();
        m.insert(
            "root".to_string(),
            Json::Str(rj.get("root").unwrap().as_str().unwrap().to_string()),
        );
    }
    let r = request(&verifier, "POST", "/verify", body.compact().as_bytes());
    let v = json::parse(&r.body).unwrap();
    assert_eq!(
        v.get("valid").and_then(Json::as_bool),
        Some(true),
        "{}",
        r.body
    );
}

#[test]
fn http_put_block_incremental_and_errors() {
    let dir = tempdir("puthttp");
    Store::create(RealVfs::new(), &dir, 4).unwrap();
    let addr = spawn_real_store(&dir);

    let r = request(&addr, "PUT", "/block?index=0", b"abcd");
    assert_eq!(r.status, 200);
    let r = request(&addr, "PUT", "/block?index=1", b"ef");
    assert_eq!(r.status, 200);

    // Appending while the last block is short is rejected (400).
    let r = request(&addr, "PUT", "/block?index=2", b"ghij");
    assert_eq!(r.status, 400);

    // Fill the last block to full, then appending works.
    let r = request(&addr, "PUT", "/block?index=1", b"efgh");
    assert_eq!(r.status, 200);
    let r = request(&addr, "PUT", "/block?index=2", b"ij");
    assert_eq!(r.status, 200);

    let r = request(&addr, "GET", "/root", b"");
    let v = json::parse(&r.body).unwrap();
    assert_eq!(v.get("data_len").and_then(Json::as_i64), Some(10));
    assert_eq!(v.get("n").and_then(Json::as_i64), Some(3));

    // Unknown route.
    let r = request(&addr, "GET", "/nope", b"");
    assert_eq!(r.status, 404);
}
