//! Shared helpers for integration tests, written with only the standard
//! library on top of the crate itself (no reqwest/tempfile needed):
//!
//! * [`TestDir`]  - a unique temp directory that cleans itself up.
//! * [`request`]  - a minimal HTTP/1.1 client over a raw `TcpStream`.
//! * [`spawn_server`] - boots the real Axum app on an ephemeral port in a
//!   background thread and returns its base URL.
#![allow(dead_code)]

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use refcount_store::{router, Store};

/// A unique temporary directory removed on drop.
pub struct TestDir {
    path: PathBuf,
}

impl TestDir {
    pub fn new() -> Self {
        let mut path = std::env::temp_dir();
        let unique = format!(
            "rcs-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        path.push(unique);
        std::fs::create_dir_all(&path).unwrap();
        TestDir { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Default for TestDir {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// A parsed HTTP response.
pub struct Resp {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
}

impl Resp {
    pub fn json(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body).expect("response body should be JSON")
    }

    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

/// Issue an HTTP/1.1 request. `body` is sent with a Content-Length when
/// non-empty. The connection is closed by the server after responding.
pub fn request(method: &str, url: &str, content_type: Option<&str>, body: &[u8]) -> Resp {
    let (host, port, path) = parse_url(url);
    let mut last_err = None;
    let mut stream = None;
    // The server may need a moment to start; retry the connect briefly.
    for _ in 0..100 {
        match TcpStream::connect((host.as_str(), port)) {
            Ok(s) => {
                stream = Some(s);
                break;
            }
            Err(e) => {
                last_err = Some(e);
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
    let mut stream = stream.unwrap_or_else(|| panic!("connect {host}:{port} failed: {:?}", last_err));
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .unwrap();

    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n");
    if !body.is_empty() {
        req.push_str(&format!("Content-Length: {}\r\n", body.len()));
        if let Some(ct) = content_type {
            req.push_str(&format!("Content-Type: {ct}\r\n"));
        }
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    stream.flush().unwrap();

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();

    parse_response(&raw)
}

pub fn get(url: &str) -> Resp {
    request("GET", url, None, &[])
}

pub fn post(url: &str, content_type: Option<&str>, body: &[u8]) -> Resp {
    request("POST", url, content_type, body)
}

pub fn put(url: &str, content_type: Option<&str>, body: &[u8]) -> Resp {
    request("PUT", url, content_type, body)
}

pub fn delete(url: &str) -> Resp {
    request("DELETE", url, None, &[])
}

fn parse_url(url: &str) -> (String, u16, String) {
    let rest = url
        .strip_prefix("http://")
        .or_else(|| url.strip_prefix("https://"))
        .unwrap_or(url);
    let (hostport, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match hostport.find(':') {
        Some(i) => (hostport[..i].to_string(), hostport[i + 1..].parse().unwrap()),
        None => (hostport.to_string(), 80),
    };
    (host, port, path.to_string())
}

fn parse_response(raw: &[u8]) -> Resp {
    let sep = b"\r\n\r\n";
    let header_end = raw
        .windows(4)
        .position(|w| w == sep)
        .expect("response should contain headers");
    let head = std::str::from_utf8(&raw[..header_end]).unwrap();
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap();
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);

    let mut headers = HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_lowercase(), v.trim().to_string());
        }
    }

    let mut body = raw[header_end + 4..].to_vec();
    if headers
        .get("transfer-encoding")
        .map(|v| v.contains("chunked"))
        .unwrap_or(false)
    {
        body = decode_chunked(&body);
    }
    Resp { status, headers, body }
}

fn decode_chunked(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut pos = 0;
    loop {
        let line_end = input[pos..]
            .windows(2)
            .position(|w| w == b"\r\n")
            .map(|p| pos + p)
            .unwrap();
        let size_str = std::str::from_utf8(&input[pos..line_end]).unwrap().trim();
        let size = usize::from_str_radix(size_str.split(';').next().unwrap(), 16).unwrap();
        pos = line_end + 2;
        if size == 0 {
            break;
        }
        out.extend_from_slice(&input[pos..pos + size]);
        pos += size + 2;
    }
    out
}

/// Boot the real app on an ephemeral port in a background OS thread with its
/// own single-threaded Tokio runtime. Returns the base URL.
pub fn spawn_server(store: Store) -> String {
    // Bind a non-blocking std listener on the calling thread (no runtime
    // needed), then convert it inside the spawned runtime.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    listener.set_nonblocking(true).unwrap();

    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let listener = tokio::net::TcpListener::from_std(listener).unwrap();
            axum::serve(listener, router(store)).await.unwrap();
        });
    });

    let base = format!("http://127.0.0.1:{port}");
    // Wait for readiness.
    for _ in 0..100 {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    base
}

/// Open a fresh store in its own temp dir and serve it.
pub fn fresh_server(retention: u64) -> (TestDir, String) {
    let dir = TestDir::new();
    let store = Store::open(dir.path(), retention).unwrap();
    let base = spawn_server(store);
    (dir, base)
}
