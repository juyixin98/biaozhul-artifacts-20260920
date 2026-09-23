//! End-to-end tests over real TCP sockets against the HTTP verification
//! server.

mod common;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use cas_repo::hash::Sha256;
use cas_repo::server::{Server, ServerConfig};
use cas_repo::store::DynStore;
use cas_repo::vfs::{StdVfs, Vfs};
use common::{b64_encode, TempDir};

struct TestServer {
    addr: std::net::SocketAddr,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
    store: Arc<DynStore>,
}

fn spawn_server() -> (TestServer, TempDir) {
    let dir = TempDir::new("http");
    let vfs: Box<dyn Vfs> = Box::new(StdVfs::new());
    let store = DynStore::open_boxed(dir.path(), vfs).unwrap();
    let server = Server::bind("127.0.0.1:0", store.clone()).unwrap();
    let addr = server.local_addr().unwrap();
    let shutdown = server.shutdown_handle();
    thread::spawn(move || server.serve(ServerConfig::default()));
    (
        TestServer {
            addr,
            shutdown,
            store,
        },
        dir,
    )
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.shutdown
            .store(true, std::sync::atomic::Ordering::Relaxed);
    }
}

struct HttpResponse {
    status: u16,
    headers: std::collections::HashMap<String, String>,
    body: Vec<u8>,
}

fn request(
    addr: std::net::SocketAddr,
    method: &str,
    path: &str,
    extra_headers: &[(&str, &str)],
    body: &[u8],
) -> HttpResponse {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {}\r\n", body.len());
    for (k, v) in extra_headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .expect("headers");
    let header_text = String::from_utf8_lossy(&raw[..split]).to_string();
    let mut lines = header_text.split("\r\n");
    let status_line = lines.next().unwrap();
    let status = status_line
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let mut headers = std::collections::HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let body_start = split + 4;
    HttpResponse {
        status,
        headers,
        body: raw[body_start..].to_vec(),
    }
}

fn json_body(r: &HttpResponse) -> cas_repo::json::Json {
    cas_repo::json::parse(std::str::from_utf8(&r.body).unwrap()).unwrap()
}

#[test]
fn health_and_stats() {
    let (srv, _dir) = spawn_server();
    let r = request(srv.addr, "GET", "/health", &[], b"");
    assert_eq!(r.status, 200);
    assert_eq!(
        json_body(&r).get("status").and_then(|j| j.as_str()),
        Some("ok")
    );

    let r = request(srv.addr, "GET", "/stats", &[], b"");
    assert_eq!(r.status, 200);
    assert_eq!(
        json_body(&r).get("blocks").and_then(|j| j.as_u64()),
        Some(0)
    );
}

#[test]
fn raw_upload_then_download_verifies_hash() {
    let (srv, _dir) = spawn_server();
    let data = b"verification data";
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/octet-stream")],
        data,
    );
    assert_eq!(r.status, 200);
    let hash = json_body(&r)
        .get("hash")
        .and_then(|j| j.as_str())
        .unwrap()
        .to_string();
    assert_eq!(
        json_body(&r)
            .get("deduplicated")
            .map(|j| matches!(j, cas_repo::json::Json::Bool(false))),
        Some(true)
    );

    // Second upload of identical bytes is deduplicated.
    let r2 = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/octet-stream")],
        data,
    );
    assert_eq!(r2.status, 200);
    assert!(matches!(
        json_body(&r2).get("deduplicated"),
        Some(cas_repo::json::Json::Bool(true))
    ));

    // Download raw bytes.
    let r3 = request(srv.addr, "GET", &format!("/blocks/{hash}"), &[], b"");
    assert_eq!(r3.status, 200);
    assert_eq!(r3.body, data);
    assert_eq!(
        r3.headers.get("x-content-sha256").map(String::as_str),
        Some(hash.as_str())
    );

    // Info endpoint.
    let r4 = request(srv.addr, "GET", &format!("/blocks/{hash}/info"), &[], b"");
    assert_eq!(r4.status, 200);
    assert_eq!(
        json_body(&r4)
            .get("present")
            .map(|j| matches!(j, cas_repo::json::Json::Bool(true))),
        Some(true)
    );
}

#[test]
fn json_upload_with_declared_hash_and_refs() {
    let (srv, _dir) = spawn_server();

    let leaf_data = b"leaf over http";
    let leaf_hash = Sha256::hash(leaf_data).to_hex();

    // Upload leaf via base64 JSON with declared hash.
    let leaf_body = format!(
        r#"{{"data_b64":"{}","hash":"{}"}}"#,
        b64_encode(leaf_data),
        leaf_hash
    );
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/json")],
        leaf_body.as_bytes(),
    );
    assert_eq!(r.status, 200);

    let manifest = b"manifest over http";
    let m_body = format!(
        r#"{{"data_b64":"{}","refs":["{}"]}}"#,
        b64_encode(manifest),
        leaf_hash
    );
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/json")],
        m_body.as_bytes(),
    );
    assert_eq!(r.status, 200);
    let m_hash = json_body(&r)
        .get("hash")
        .and_then(|j| j.as_str())
        .unwrap()
        .to_string();

    let r = request(srv.addr, "GET", &format!("/blocks/{m_hash}/info"), &[], b"");
    let info = json_body(&r);
    let refs = info.get("refs").and_then(|j| j.as_array()).unwrap();
    assert_eq!(refs[0].as_str(), Some(leaf_hash.as_str()));
}

#[test]
fn rejected_hash_mismatch_over_http() {
    let (srv, _dir) = spawn_server();
    let body = format!(
        r#"{{"data_b64":"{}","hash":"{}"}}"#,
        b64_encode(b"actual content"),
        Sha256::hash(b"declared different").to_hex()
    );
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/json")],
        body.as_bytes(),
    );
    assert_eq!(r.status, 422);
    assert_eq!(
        json_body(&r).get("error").and_then(|j| j.as_str()),
        Some("hash_mismatch")
    );
}

#[test]
fn root_lifecycle_and_occ_over_http() {
    let (srv, _dir) = spawn_server();

    let a = Sha256::hash(b"A").to_hex();
    let b = Sha256::hash(b"B").to_hex();
    let upload = |s: &str| {
        let body = format!(r#"{{"data_b64":"{}"}}"#, b64_encode(s.as_bytes()));
        let r = request(
            srv.addr,
            "POST",
            "/blocks",
            &[("Content-Type", "application/json")],
            body.as_bytes(),
        );
        assert_eq!(r.status, 200);
    };
    upload("A");
    upload("B");

    let r = request(
        srv.addr,
        "PUT",
        "/roots/main",
        &[("Content-Type", "application/json")],
        format!(r#"{{"hash":"{a}"}}"#).as_bytes(),
    );
    assert_eq!(r.status, 200);
    assert_eq!(
        json_body(&r).get("version").and_then(|j| j.as_u64()),
        Some(1)
    );

    // Correct If-Match on the current version: succeeds, bumps version.
    let r = request(
        srv.addr,
        "PUT",
        "/roots/main",
        &[("Content-Type", "application/json"), ("If-Match", "1")],
        format!(r#"{{"hash":"{b}"}}"#).as_bytes(),
    );
    assert_eq!(r.status, 200);
    assert_eq!(
        json_body(&r).get("version").and_then(|j| j.as_u64()),
        Some(2)
    );

    // Stale If-Match -> 409.
    let r = request(
        srv.addr,
        "PUT",
        "/roots/main",
        &[("Content-Type", "application/json"), ("If-Match", "1")],
        format!(r#"{{"hash":"{a}"}}"#).as_bytes(),
    );
    assert_eq!(r.status, 409);

    // Correct If-Match again.
    let r = request(
        srv.addr,
        "PUT",
        "/roots/main",
        &[("Content-Type", "application/json"), ("If-Match", "2")],
        format!(r#"{{"hash":"{a}"}}"#).as_bytes(),
    );
    assert_eq!(r.status, 200);
    assert_eq!(
        json_body(&r).get("version").and_then(|j| j.as_u64()),
        Some(3)
    );

    let r = request(srv.addr, "GET", "/roots", &[], b"");
    assert_eq!(r.status, 200);
    let listing = json_body(&r);
    let arr = listing.as_array().unwrap();
    assert_eq!(arr.len(), 1);

    let r = request(srv.addr, "DELETE", "/roots/main", &[], b"");
    assert_eq!(r.status, 200);
    let r = request(srv.addr, "GET", "/roots/main", &[], b"");
    assert_eq!(r.status, 404);
}

#[test]
fn gc_endpoint_collects_orphan_reports_missing() {
    let (srv, _dir) = spawn_server();

    // Live block under a root.
    let live = b"reachable";
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/octet-stream")],
        live,
    );
    let live_hash = json_body(&r)
        .get("hash")
        .and_then(|j| j.as_str())
        .unwrap()
        .to_string();
    let r = request(srv.addr, "PUT", "/roots/main", &[], live_hash.as_bytes());
    assert_eq!(r.status, 200);

    // Orphan block.
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/octet-stream")],
        b"orphan over http",
    );
    assert_eq!(r.status, 200);

    // Block referencing a ghost.
    let ghost = Sha256::hash(b"ghost").to_hex();
    let parent_body = format!(
        r#"{{"data_b64":"{}","refs":["{ghost}"]}}"#,
        b64_encode(b"parent-with-ghost")
    );
    let r = request(
        srv.addr,
        "POST",
        "/blocks",
        &[("Content-Type", "application/json")],
        parent_body.as_bytes(),
    );
    // Parent itself is an orphan (no root), so it will be collected; nothing
    // reachable references the ghost in this setup. Put it under a second
    // root instead to observe a missing-reference report.
    let parent_hash = json_body(&r)
        .get("hash")
        .and_then(|j| j.as_str())
        .unwrap()
        .to_string();
    let r = request(srv.addr, "PUT", "/roots/other", &[], parent_hash.as_bytes());
    assert_eq!(r.status, 200);

    let r = request(srv.addr, "POST", "/gc", &[], b"");
    assert_eq!(r.status, 200);
    let rep = json_body(&r);
    assert_eq!(rep.get("blocks_removed").and_then(|j| j.as_u64()), Some(1));
    assert_eq!(
        rep.get("blocks_live").and_then(|j| j.as_u64()),
        Some(2),
        "live root block + ghost-referencing parent"
    );
    let missing = rep
        .get("missing_references")
        .and_then(|j| j.as_array())
        .unwrap();
    assert_eq!(missing.len(), 1);
    assert_eq!(
        missing[0].get("missing").and_then(|j| j.as_str()),
        Some(ghost.as_str())
    );

    // Live block still retrievable.
    let r = request(srv.addr, "GET", &format!("/blocks/{live_hash}"), &[], b"");
    assert_eq!(r.status, 200);
    assert_eq!(r.body, live);
}

#[test]
fn concurrent_http_uploads_same_block() {
    let (srv, _dir) = spawn_server();
    let data = vec![7u8; 128 * 1024];
    let threads = 12;
    let barrier = Arc::new(std::sync::Barrier::new(threads));
    let mut handles = Vec::new();
    for _ in 0..threads {
        let data = data.clone();
        let barrier = barrier.clone();
        let addr = srv.addr;
        handles.push(thread::spawn(move || {
            barrier.wait();
            request(
                addr,
                "POST",
                "/blocks",
                &[("Content-Type", "application/octet-stream")],
                &data,
            )
        }));
    }
    let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    for r in &results {
        assert_eq!(r.status, 200);
    }
    let hashes: Vec<String> = results
        .iter()
        .map(|r| {
            json_body(r)
                .get("hash")
                .and_then(|j| j.as_str())
                .unwrap()
                .to_string()
        })
        .collect();
    let unique: std::collections::HashSet<_> = hashes.iter().collect();
    assert_eq!(unique.len(), 1, "all uploads address the same digest");

    let stats = srv.store.stats().unwrap();
    assert_eq!(stats.blocks, 1, "exactly one blob on disk");
}

#[test]
fn unknown_route_404() {
    let (srv, _dir) = spawn_server();
    let r = request(srv.addr, "GET", "/nope", &[], b"");
    assert_eq!(r.status, 404);
}
