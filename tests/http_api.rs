//! End-to-end HTTP tests. They bind the real Axum server on an ephemeral
//! port and speak raw HTTP/1.1 over TCP, so no client crate is needed.
use ext_hash_index::server::router;
use ext_hash_index::{Config, HashKind, Index};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

fn tmp(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "ext-hash-http-{}-{}-{}",
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

struct Server {
    addr: String,
    _rt: tokio::runtime::Runtime,
}

fn spawn_index(cfg: Config) -> (Server, PathBuf) {
    let dir = tmp("srv");
    let path = dir.join("http.db");
    let idx = Index::create(&path, &cfg).unwrap();
    let app = router(idx);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let listener = rt
        .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
        .unwrap();
    let addr = listener.local_addr().unwrap();
    rt.spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    std::thread::sleep(Duration::from_millis(50));
    (
        Server {
            addr: addr.to_string(),
            _rt: rt,
        },
        path,
    )
}

fn request(addr: &str, method: &str, path: &str, body: Option<&str>) -> (u16, String) {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let body = body.unwrap_or("");
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len}\r\nConnection: close\r\n\r\n{body}",
        method = method,
        path = path,
        len = body.len(),
        body = body
    );
    stream.write_all(req.as_bytes()).unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let text = String::from_utf8_lossy(&raw);
    let status: u16 = text
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let json = text
        .split("\r\n\r\n")
        .nth(1)
        .map(|s| s.to_string())
        .unwrap_or_default();
    (status, json)
}

#[test]
fn http_health_stats_and_crud() {
    let (srv, _path) = spawn_index(Config {
        bucket_capacity: 4,
        max_depth: 20,
        hash: HashKind::U64LowBits,
        key_max: 128,
        val_max: 256,
    });
    let a = &srv.addr;

    let (st, body) = request(a, "GET", "/health", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"ok\""));

    // Insert two keys.
    let (st, body) = request(a, "PUT", "/keys/1", Some(r#"{"value":"one"}"#));
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"inserted\":true"));
    let (st, body) = request(a, "PUT", "/keys/2", Some(r#"{"value":"two"}"#));
    assert_eq!(st, 200, "{}", body);

    // Overwrite -> inserted false.
    let (st, body) = request(a, "PUT", "/keys/1", Some(r#"{"value":"ONE"}"#));
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"inserted\":false"));

    // Get.
    let (st, body) = request(a, "GET", "/keys/1", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"found\":true") && body.contains("ONE"));
    let (st, body) = request(a, "GET", "/keys/99", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"found\":false"));

    // Delete.
    let (st, body) = request(a, "DELETE", "/keys/2", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"deleted\":true"));
    let (st, _) = request(a, "GET", "/keys/2", None);
    assert_eq!(st, 200);

    // Stats reflect the structure.
    let (st, body) = request(a, "GET", "/stats", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"global_depth\":0"));
    assert!(body.contains("\"bucket_capacity\":4"));
    assert!(body.contains("u64lowbits"));
}

#[test]
fn http_collision_returns_explicit_507() {
    let (srv, _path) = spawn_index(Config {
        bucket_capacity: 2,
        max_depth: 20,
        hash: HashKind::Constant,
        key_max: 128,
        val_max: 256,
    });
    let a = &srv.addr;

    for k in ["a", "b"] {
        let (st, body) = request(
            a,
            "PUT",
            &format!("/keys/{}", k),
            Some(&format!("{{\"value\":\"{}\"}}", k)),
        );
        assert_eq!(st, 200, "{}", body);
    }
    // Third distinct key, constant hash, full bucket -> 507 with clear code.
    let (st, body) = request(a, "PUT", "/keys/c", Some(r#"{"value":"c"}"#));
    assert_eq!(st, 507, "expected 507, body={}", body);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["code"], "hash_collision_capacity");
    assert!(
        v["error"].as_str().unwrap().contains("capacity exhausted"),
        "{}",
        v["error"]
    );
    assert!(v["error"].as_str().unwrap().contains("collides"));

    // The failed insert must not be observable.
    let (st, body) = request(a, "GET", "/keys/c", None);
    assert_eq!(st, 200);
    assert!(body.contains("\"found\":false"));
}

#[test]
fn http_value_too_large_is_413() {
    let (srv, _path) = spawn_index(Config {
        bucket_capacity: 4,
        max_depth: 20,
        hash: HashKind::Fnv1a64,
        key_max: 4,
        val_max: 8,
    });
    let a = &srv.addr;
    let (st, _) = request(a, "PUT", "/keys/toolongkey", Some(r#"{"value":"v"}"#));
    assert_eq!(st, 413);
    let (st, _) = request(a, "PUT", "/keys/k", Some(r#"{"value":"0123456789"}"#));
    assert_eq!(st, 413);
}

#[test]
fn http_reopen_recovers_and_serves() {
    // Crash-split a file via crash-runner, then serve it: first open replays
    // the intent and the server reports recovered_intent.
    let dir = tmp("reopen");
    let path = dir.join("r.db");
    let bin = env!("CARGO_BIN_EXE_crash-runner");
    for k in 0u64..4 {
        let out = std::process::Command::new(bin)
            .args([
                "--file",
                path.to_str().unwrap(),
                "--op",
                "put",
                "--key",
                &k.to_string(),
                "--value",
                "v",
                "--capacity",
                "4",
                "--hash",
                "u64lowbits",
                "--crash",
                "__none__",
            ])
            .output()
            .unwrap();
        assert!(out.status.success());
    }
    let out = std::process::Command::new(bin)
        .args([
            "--file",
            path.to_str().unwrap(),
            "--op",
            "put",
            "--key",
            "4",
            "--value",
            "v",
            "--capacity",
            "4",
            "--hash",
            "u64lowbits",
            "--crash",
            "split.after_directory",
        ])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(9));

    let idx = Index::open(&path).unwrap();
    assert_eq!(idx.recovered_intent(), Some("split"));
    let app = router(idx);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();
    let listener = rt
        .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
        .unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    rt.spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    std::thread::sleep(Duration::from_millis(50));

    let (st, body) = request(&addr, "GET", "/stats", None);
    assert_eq!(st, 200, "{}", body);
    assert!(body.contains("\"recovered_intent\":\"split\""));
    // Pre-crash keys readable; post-crash key absent.
    let (st, body) = request(&addr, "GET", "/keys/0", None);
    assert_eq!(st, 200);
    assert!(body.contains("\"found\":true"));
    let (st, body) = request(&addr, "GET", "/keys/4", None);
    assert_eq!(st, 200);
    assert!(body.contains("\"found\":false"));
}
