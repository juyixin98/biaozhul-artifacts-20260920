//! Test harness: builds the binary once, launches it on an ephemeral port,
//! and exposes tiny JSON helpers over plain `std::net` HTTP (no extra dev-deps).

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command};
use std::sync::Once;
use std::time::Duration;

use serde_json::{json, Value};

static BUILD_ONCE: Once = Once::new();

pub struct Server {
    pub port: u16,
    _child: Child,
}

impl Server {
    pub fn start() -> Server {
        BUILD_ONCE.call_once(|| {
            let status = Command::new(env!("CARGO"))
                .args(["build", "--bin", "depres"])
                .status()
                .expect("build depres binary");
            assert!(status.success(), "pre-test build failed");
        });

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);

        let bin = env!("CARGO_BIN_EXE_depres");
        let child = Command::new(bin)
            .env("DEPRES_ADDR", format!("127.0.0.1:{port}"))
            .spawn()
            .expect("spawn server");

        let srv = Server { port, _child: child };
        // Wait for /health.
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            if std::time::Instant::now() > deadline {
                panic!("server did not become ready");
            }
            if let Ok(mut stream) = TcpStream::connect(format!("127.0.0.1:{port}")) {
                if stream
                    .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    .is_ok()
                {
                    let mut buf = String::new();
                    if stream.read_to_string(&mut buf).is_ok() && buf.contains("\"ok\"") {
                        break;
                    }
                }
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        srv
    }

    fn request(&self, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
        let body_bytes = match body {
            Some(v) => serde_json::to_vec(&v).unwrap(),
            None => Vec::new(),
        };
        let mut last_err = None;
        for _ in 0..20 {
            match TcpStream::connect(format!("127.0.0.1:{}", self.port)) {
                Ok(mut stream) => {
                    stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
                    let head = format!(
                        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body_bytes.len()
                    );
                    let mut req = head.into_bytes();
                    req.extend_from_slice(&body_bytes);
                    stream.write_all(&req).unwrap();
                    let mut raw = Vec::new();
                    stream.read_to_end(&mut raw).unwrap();
                    let text = String::from_utf8_lossy(&raw);
                    let split = text.find("\r\n\r\n").expect("response headers");
                    let status_line = text.lines().next().unwrap();
                    let status: u16 = status_line.split_whitespace().nth(1).unwrap().parse().unwrap();
                    // Handle chunked transfer encoding just in case; axum uses
                    // Content-Length for Json, but be defensive.
                    let payload = &raw[split + 4..];
                    let value: Value = serde_json::from_slice(handle_chunked(payload)).unwrap_or_else(|e| {
                        panic!("json parse error {e}; body={}", String::from_utf8_lossy(payload))
                    });
                    return (status, value);
                }
                Err(e) => last_err = Some(e),
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("could not connect: {last_err:?}");
    }

    pub fn get_json(&self, path: &str) -> Value {
        self.request("GET", path, None).1
    }

    pub fn solve(&self, req: &Value) -> Value {
        let mut req = req.clone();
        // Turn the brute-force oracle on for every test unless it explicitly
        // passes `{"verify": false}` (fixtures round-tripped from the server
        // carry the serde default `false`, so this is not just an is_none check).
        // Always cross-check with the brute-force oracle in tests. Fixtures
        // fetched from the server carry the serde default `verify: false`;
        // overwrite unconditionally.
        req["verify"] = json!(true);
        self.request("POST", "/solve", Some(req)).1
    }

    pub fn replay(&self, body: Value) -> Value {
        self.request("POST", "/lock/replay", Some(body)).1
    }

    pub fn fixture(&self, name: &str) -> Value {
        self.get_json(&format!("/fixtures/{name}"))
    }
}

fn handle_chunked(payload: &[u8]) -> &[u8] {
    // Axum sets Content-Length; if we ever see a chunk marker, fall back to
    // the naive interpretation used only by tests — keep the raw slice.
    payload
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self._child.kill();
    }
}
