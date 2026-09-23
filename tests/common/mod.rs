//! Shared test helpers: isolated temp data directories and a tiny synchronous
//! HTTP client (uses only std, like the rest of the crate).

#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use mvcc_reclaim::json::Json;

static COUNTER: AtomicU64 = AtomicU64::new(0);

pub fn temp_dir(tag: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let p = std::env::temp_dir().join(format!(
        "mvcc-reclaim-test-{}-{}-{}",
        tag,
        std::process::id(),
        n
    ));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

pub struct HttpResponse {
    pub status: u16,
    pub body: Json,
    pub raw: String,
}

pub fn http(addr: &str, method: &str, path: &str, body: &str) -> HttpResponse {
    let mut stream = TcpStream::connect(addr).expect("connect");
    let req = if body.is_empty() {
        format!("{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n")
    } else {
        format!(
            "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    };
    stream.write_all(req.as_bytes()).unwrap();
    let mut raw = String::new();
    stream.read_to_string(&mut raw).unwrap();

    let idx = raw.find("\r\n\r\n").expect("response split");
    let status = raw
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body_text = &raw[idx + 4..];
    let json = Json::from_bytes(body_text.as_bytes()).unwrap_or(Json::Null);
    HttpResponse {
        status,
        body: json,
        raw: body_text.to_string(),
    }
}

pub fn int(j: &Json, key: &str) -> i64 {
    match j.get(key) {
        Some(Json::Int(n)) => *n,
        other => panic!("expected int field {key}, got {other:?}"),
    }
}
