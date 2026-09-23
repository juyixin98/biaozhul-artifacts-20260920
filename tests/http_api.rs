//! HTTP API integration test: real server on an ephemeral port, real
//! TcpStream requests, verifying the local validation entry end to end.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tsblock::http;
use tsblock::io::FsIO;
use tsblock::storage::{LateMode, Repo};

fn test_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("tsblock-http-{}-{}", std::process::id(), name));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn request(addr: &str, req: &str) -> String {
    let mut s = TcpStream::connect(addr).unwrap();
    s.write_all(req.as_bytes()).unwrap();
    let mut buf = String::new();
    s.read_to_string(&mut buf).unwrap();
    buf
}

fn body_of(response: &str) -> &str {
    response.split("\r\n\r\n").nth(1).unwrap_or("")
}

#[test]
fn http_end_to_end() {
    let dir = test_dir("e2e");
    let repo = Repo::open(FsIO::new(&dir), LateMode::Reject, 4).unwrap();
    let repo = Arc::new(Mutex::new(repo));
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    {
        let repo = Arc::clone(&repo);
        std::thread::spawn(move || {
            let _ = http::serve_listener(repo, listener);
        });
    }

    // health
    let r = request(&addr, "GET /health HTTP/1.1\r\nHost: x\r\n\r\n");
    assert!(r.starts_with("HTTP/1.1 200"), "{}", r);
    assert!(body_of(&r).contains("\"ok\":true"));

    // ingest: 6 in-order points + 1 duplicate + 1 out-of-order
    let ingest_body = r#"{"series":"cpu","points":[[1000,5],[1001,7],[1002,-3],[1003,0],[1004,11],[1005,-8],[1005,99],[1002,1]]}"#;
    let req = format!(
        "POST /ingest HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{}",
        ingest_body.len(),
        ingest_body
    );
    let r = request(&addr, &req);
    let body = body_of(&r).to_string();
    assert!(body.contains("\"accepted\":6"), "{}", body);
    assert!(body.contains("\"reason\":\"duplicate\""), "{}", body);
    assert!(body.contains("\"reason\":\"out_of_order\""), "{}", body);

    // flush
    let flush_body = r#"{"series":"cpu"}"#;
    let req = format!(
        "POST /flush HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{}",
        flush_body.len(),
        flush_body
    );
    let r = request(&addr, &req);
    assert!(body_of(&r).contains("\"flushed\":true"), "{}", r);

    // query full range
    let r = request(&addr, "GET /query?series=cpu&from=0&to=10000 HTTP/1.1\r\nHost: x\r\n\r\n");
    let body = body_of(&r).to_string();
    assert!(body.contains("\"count\":6"), "{}", body);
    assert!(body.contains("[1002,-3]"), "{}", body);
    assert!(body.contains("[1005,-8]"), "{}", body);
    assert!(!body.contains("99"), "rejected point must not appear: {}", body);

    // boundary query: exactly one point
    let r = request(&addr, "GET /query?series=cpu&from=1002&to=1002 HTTP/1.1\r\nHost: x\r\n\r\n");
    let body = body_of(&r).to_string();
    assert!(body.contains("\"count\":1"), "{}", body);
    assert!(body.contains("[[1002,-3]]"), "{}", body);

    // invalid range
    let r = request(&addr, "GET /query?series=cpu&from=9&to=1 HTTP/1.1\r\nHost: x\r\n\r\n");
    assert!(r.starts_with("HTTP/1.1 400"), "{}", r);

    // stats
    let r = request(&addr, "GET /stats?series=cpu HTTP/1.1\r\nHost: x\r\n\r\n");
    let body = body_of(&r).to_string();
    assert!(body.contains("\"points\":6"), "{}", body);
    assert!(body.contains("\"raw_bytes\":96"), "{}", body);
    assert!(body.contains("\"compression_ratio\":"), "{}", body);

    // 404
    let r = request(&addr, "GET /nope HTTP/1.1\r\nHost: x\r\n\r\n");
    assert!(r.starts_with("HTTP/1.1 404"), "{}", r);

    // bad JSON
    let bad = "{not json";
    let req = format!(
        "POST /ingest HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n{}",
        bad.len(),
        bad
    );
    let r = request(&addr, &req);
    assert!(r.starts_with("HTTP/1.1 400"), "{}", r);

    // persistence: repo state survived on disk; a fresh repo sees the data
    let repo2 = Repo::open(FsIO::new(&dir), LateMode::Reject, 4).unwrap();
    assert_eq!(
        repo2.query("cpu", 0, 10000).unwrap(),
        vec![
            (1000, 5),
            (1001, 7),
            (1002, -3),
            (1003, 0),
            (1004, 11),
            (1005, -8)
        ]
    );
}
