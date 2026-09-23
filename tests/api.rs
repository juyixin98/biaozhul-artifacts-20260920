//! End-to-end HTTP tests: a real Axum server on an ephemeral port, driven by
//! a minimal raw HTTP/1.1 client (no external test HTTP framework). Every
//! proof returned over the wire is cross-checked with the independent
//! reference verifier in `common/independent.rs`.

#[path = "common/independent.rs"]
mod independent;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

use independent::{check_response, Response as RefResponse};
use merkle_proof_service::{api, Service};
use tempfile::TempDir;

fn hex(s: &str) -> String {
    hex::encode(s.as_bytes())
}

fn spawn_server() -> (String, TempDir) {
    let dir = TempDir::new().unwrap();
    let db_path: PathBuf = dir.path().to_path_buf();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let service = Service::open(&db_path).unwrap();
            let app = api::router(service);
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            tx.send(listener.local_addr().unwrap().port()).unwrap();
            axum::serve(listener, app).await.unwrap();
        });
    });
    let port = rx.recv().unwrap();
    (format!("http://127.0.0.1:{port}"), dir)
}

struct RawResponse {
    status: u16,
    body: serde_json::Value,
}

fn request(base: &str, method: &str, path: &str, body: Option<&str>) -> RawResponse {
    let addr = base.strip_prefix("http://").unwrap();
    let mut stream = TcpStream::connect(addr).unwrap();
    // Bounded read timeout; WouldBlock below is retried (transient under
    // SO_RCVTIMEO), EOF ends the read (we always send Connection: close).
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();

    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n");
    if let Some(b) = body {
        req.push_str(&format!("Content-Type: application/json\r\nContent-Length: {}\r\n", b.len()));
        req.push_str("\r\n");
        req.push_str(b);
    } else {
        req.push_str("\r\n");
    }
    stream.write_all(req.as_bytes()).unwrap();

    let mut raw = Vec::new();
    let mut buf = [0u8; 4096];
    let mut idle_ms = 0u64;
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                idle_ms = 0;
            }
            Err(ref e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::Interrupted =>
            {
                std::thread::sleep(Duration::from_millis(5));
                idle_ms += 5;
                assert!(idle_ms < 30_000, "server stopped responding on {method} {path}");
            }
            Err(e) => panic!("read error after request {method} {path}: {e}"),
        }
    }
    let text = String::from_utf8_lossy(&raw);
    let (head, rest) = text.split_once("\r\n\r\n").expect("http head");
    let status: u16 = head.split_whitespace().nth(1).unwrap().parse().unwrap();
    // Handle chunked transfer (rare with Connection: close, but be robust).
    let body_str = if head.to_ascii_lowercase().contains("transfer-encoding: chunked") {
        dechunk(rest)
    } else {
        rest.to_string()
    };
    let body = if body_str.trim().is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_str(&body_str).unwrap_or_else(|e| {
            panic!("bad JSON ({status}): {e}: {body_str:?}")
        })
    };
    RawResponse { status, body }
}

fn dechunk(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let line_end = bytes[i..].windows(2).position(|w| w == b"\r\n").map(|p| i + p);
        let Some(end) = line_end else { break };
        let size = usize::from_str_radix(std::str::from_utf8(&bytes[i..end]).unwrap().trim(), 16).unwrap();
        i = end + 2;
        out.extend_from_slice(&bytes[i..i + size]);
        i += size + 2;
    }
    String::from_utf8(out).unwrap()
}

fn post(base: &str, path: &str, body: serde_json::Value) -> RawResponse {
    let s = body.to_string();
    request(base, "POST", path, Some(&s))
}

fn crosscheck(resp: &serde_json::Value, root_hex: &str) {
    let reference: RefResponse = serde_json::from_value(resp.clone()).unwrap();
    check_response(&reference, root_hex).expect("independent verifier accepts");
}

#[test]
fn http_health_and_info_empty() {
    let (base, _dir) = spawn_server();
    let r = request(&base, "GET", "/healthz", None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["status"], "ok");
    assert_eq!(r.body["current_version"], 0);

    let r = request(&base, "GET", "/v1/info", None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["current_version"], 0);
    assert_eq!(
        r.body["current_root"].as_str().unwrap(),
        "dbc1b4c900ffe48d575b5da5c638040125f65db0fe3e24494b76ea986457d986"
    );
}

#[test]
fn http_batch_proofs_and_verify_lifecycle() {
    let (base, _dir) = spawn_server();

    // First batch: a, b, c plus an empty-valued key e.
    let batch = serde_json::json!({
        "ops": [
            {"op": "put", "key": hex("a"), "value": hex("1")},
            {"op": "put", "key": hex("b"), "value": hex("2")},
            {"op": "put", "key": hex("c"), "value": hex("3")},
            {"op": "put", "key": hex("dup"), "value": hex("first")},
            {"op": "put", "key": hex("dup"), "value": hex("last")},
            {"op": "put", "key": hex("empty"), "value": ""},
        ]
    });
    let r = post(&base, "/v1/batches", batch);
    assert_eq!(r.status, 200, "{:?}", r.body);
    assert_eq!(r.body["version"], 1);
    assert_eq!(r.body["leaf_count"], 5);
    let root1 = r.body["root"].as_str().unwrap().to_string();

    // Direct gets: present, empty value present, absent.
    let r = request(&base, "GET", &format!("/v1/keys/{}", hex("dup")), None);
    assert_eq!(r.body["exists"], true);
    assert_eq!(r.body["value"].as_str().unwrap(), hex("last"));
    let r = request(&base, "GET", &format!("/v1/keys/{}", hex("empty")), None);
    assert_eq!(r.body["exists"], true);
    assert_eq!(r.body["value"], "");
    let r = request(&base, "GET", &format!("/v1/keys/{}", hex("ghost")), None);
    assert_eq!(r.body["exists"], false);
    assert!(r.body.get("value").is_none() || r.body["value"].is_null());

    // Existence proof + independent cross-check.
    let r = request(&base, "GET", &format!("/v1/proofs/key/{}", hex("b")), None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["exists"], true);
    crosscheck(&r.body, &root1);

    // Non-existence proof (interior gap) + cross-check.
    let r = request(&base, "GET", &format!("/v1/proofs/key/{}", hex("bb")), None);
    assert_eq!(r.body["exists"], false);
    crosscheck(&r.body, &root1);

    // Edge keys too.
    for q in ["\x00", "zzzz"] {
        let r = request(&base, "GET", &format!("/v1/proofs/key/{}", hex(q)), None);
        crosscheck(&r.body, &root1);
    }

    // /v1/verify: valid proof.
    let verify_req = serde_json::json!({"root": root1, "response": r_body_proof(&base, "b")});
    let r = post(&base, "/v1/verify", verify_req);
    assert_eq!(r.body["valid"], true);

    // Tampered value -> invalid.
    let mut bad = r_body_proof(&base, "b");
    bad["value"] = serde_json::json!(hex("WRONG"));
    bad["proof"]["entry"]["value"] = serde_json::json!(hex("WRONG"));
    let r = post(&base, "/v1/verify", serde_json::json!({"root": root1, "response": bad}));
    assert_eq!(r.body["valid"], false);
    assert!(r.body["reason"].is_string());

    // Second batch changes the root; the old proof must not verify under the
    // new root and vice versa.
    let r = post(
        &base,
        "/v1/batches",
        serde_json::json!({"ops": [{"op": "delete", "key": hex("c")}]}),
    );
    let root2 = r.body["root"].as_str().unwrap().to_string();
    assert_ne!(root1, root2);

    // Proof of "b" at VERSION 1 must not verify under the new root.
    let old_proof = request(
        &base,
        "GET",
        &format!("/v1/proofs/key/{}?version=1", hex("b")),
        None,
    )
    .body;
    let r = post(&base, "/v1/verify", serde_json::json!({"root": root2, "response": old_proof}));
    assert_eq!(r.body["valid"], false);

    // ... and the current proof does not verify under the old root either.
    let new_proof = r_body_proof(&base, "b");
    let r = post(&base, "/v1/verify", serde_json::json!({"root": root1, "response": new_proof}));
    assert_eq!(r.body["valid"], false);

    // Historical version 1 still proves and verifies.
    let r = request(
        &base,
        "GET",
        &format!("/v1/proofs/key/{}?version=1", hex("c")),
        None,
    );
    assert_eq!(r.status, 200);
    assert_eq!(r.body["exists"], true);
    crosscheck(&r.body, &root1);

    // Under version 2, c is missing.
    let r = request(
        &base,
        "GET",
        &format!("/v1/proofs/key/{}?version=2", hex("c")),
        None,
    );
    assert_eq!(r.body["exists"], false);
    crosscheck(&r.body, &root2);

    // Version listing and root lookup.
    let r = request(&base, "GET", "/v1/versions", None);
    assert_eq!(r.body["versions"].as_array().unwrap().len(), 2);
    let r = request(&base, "GET", "/v1/roots/1", None);
    assert_eq!(r.body["root"].as_str().unwrap(), root1);
}

fn r_body_proof(base: &str, key: &str) -> serde_json::Value {
    request(base, "GET", &format!("/v1/proofs/key/{}", hex(key)), None).body
}

#[test]
fn http_errors_are_sensible() {
    let (base, _dir) = spawn_server();

    // Unknown version.
    let r = request(
        &base,
        "GET",
        &format!("/v1/proofs/key/{}?version=99", hex("x")),
        None,
    );
    assert_eq!(r.status, 404);
    assert_eq!(r.body["error"], "unknown_version");

    // Malformed hex key.
    let r = request(&base, "GET", "/v1/keys/zz", None);
    assert_eq!(r.status, 400);

    // Malformed batch JSON shape.
    let r = post(&base, "/v1/batches", serde_json::json!({"ops": [{"op": "frobnicate"}]}));
    assert_eq!(r.status, 400);

    // Unknown root lookup.
    let r = request(&base, "GET", "/v1/roots/5", None);
    assert_eq!(r.status, 404);
}

#[test]
fn http_server_restart_keeps_history() {
    // Cold restart: shut the first server all the way down (releasing the
    // RocksDB lock), then reopen the database as a fresh process would.
    let dir = TempDir::new().unwrap();
    let db_path: PathBuf = dir.path().to_path_buf();
    let (base, shutdown, handle) = serve_at(&db_path);

    let r = post(
        &base,
        "/v1/batches",
        serde_json::json!({"ops": [{"op": "put", "key": hex("k1"), "value": hex("v1")}]}),
    );
    assert_eq!(r.status, 200);
    let root_v1 = r.body["root"].as_str().unwrap().to_string();

    // Stop the server and wait for its thread (and DB handle) to finish.
    shutdown.send(()).unwrap();
    handle.join().unwrap();

    // Reopen exactly as a restarted process would.
    let store = merkle_proof_service::Store::open(&db_path).unwrap();
    assert_eq!(store.current_version().unwrap(), 1);
    assert_eq!(store.version_info(1).unwrap().unwrap().root[..], hex::decode(&root_v1).unwrap()[..]);
    assert_eq!(store.get_current(b"k1").unwrap(), Some(b"v1".to_vec()));
}

fn serve_at(
    path: &std::path::Path,
) -> (String, std::sync::mpsc::Sender<()>, std::thread::JoinHandle<()>) {
    let db_path = path.to_path_buf();
    let (tx_port, rx_port) = std::sync::mpsc::channel();
    let (tx_stop, rx_stop) = std::sync::mpsc::channel::<()>();
    let handle = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap();
        rt.block_on(async move {
            let service = Service::open(&db_path).unwrap();
            let app = api::router(service);
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            tx_port.send(listener.local_addr().unwrap().port()).unwrap();
            axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    let _ = rx_stop.recv();
                })
                .await
                .unwrap();
        });
    });
    let port = rx_port.recv().unwrap();
    (format!("http://127.0.0.1:{port}"), tx_stop, handle)
}
