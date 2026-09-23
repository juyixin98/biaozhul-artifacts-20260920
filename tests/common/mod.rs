//! Shared test utilities.
//!
//! Repository-level tests run against *both* backends:
//! - [`MemFs`] — fast, deterministic, supports injected faults;
//! - [`RealFs`] on a temp directory — proves the actual on-disk format and
//!   process-level locking work.

#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use cas_store::{Fault, FaultFs, MemFs, RealFs, Repository, Vfs};

/// Distinct bytes -> distinct hashes.
pub fn block(seed: u8, size: usize) -> Vec<u8> {
    let mut v = vec![seed; size];
    // Mix in the position so equal-sized seeds don't accidentally collide.
    for (i, b) in v.iter_mut().enumerate() {
        *b = b.wrapping_add((i % 251) as u8);
    }
    v
}

/// RAII temp directory removed on drop.
pub struct TempDir {
    pub path: PathBuf,
}

impl TempDir {
    pub fn new(label: &str) -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "cas-test-{label}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&path).unwrap();
        TempDir { path }
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// One repository under test, with its supporting state.
pub struct RepoHarness {
    pub repo: Repository,
    /// Kept alive for the test (FaultFs wraps it; MemFs is shared).
    pub vfs: Arc<dyn Vfs>,
    pub temp: Option<TempDir>,
}

/// Open a fresh in-memory repository.
pub fn mem_repo() -> RepoHarness {
    let vfs: Arc<dyn Vfs> = Arc::new(MemFs::new());
    let repo = Repository::open("/repo", vfs.clone()).unwrap();
    RepoHarness {
        repo,
        vfs,
        temp: None,
    }
}

/// Open a fresh on-disk repository.
pub fn real_repo(label: &str) -> RepoHarness {
    let temp = TempDir::new(label);
    let vfs: Arc<dyn Vfs> = Arc::new(RealFs::new());
    let repo = Repository::open(temp.path.join("store"), vfs.clone()).unwrap();
    RepoHarness {
        repo,
        vfs,
        temp: Some(temp),
    }
}

/// In-memory repository whose I/O layer injects `faults`.
pub fn fault_repo(faults: Vec<Fault>) -> RepoHarness {
    let mem = Arc::new(MemFs::new());
    let vfs: Arc<dyn Vfs> = Arc::new(FaultFs::new(mem, faults));
    let repo = Repository::open("/repo", vfs.clone()).unwrap();
    RepoHarness {
        repo,
        vfs,
        temp: None,
    }
}

/// Run a test closure against both backends.
pub fn both_backends<F>(mut f: F)
where
    F: FnMut(&mut RepoHarness),
{
    let mut h = mem_repo();
    f(&mut h);
    drop(h);
    let mut h = real_repo("both");
    f(&mut h);
}

// ---------------------------------------------------------------------------
// HTTP test harness
// ---------------------------------------------------------------------------

pub struct HttpServer {
    pub addr: String,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
}

impl HttpServer {
    pub fn start(repo: Repository) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap().to_string();
        let shutdown = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let s2 = shutdown.clone();
        thread::spawn(move || {
            listener.set_nonblocking(true).unwrap();
            use std::io::ErrorKind;
            let repo = Arc::new(repo);
            loop {
                if s2.load(std::sync::atomic::Ordering::Relaxed) {
                    break;
                }
                match listener.accept() {
                    Ok((stream, _)) => {
                        let repo = Arc::clone(&repo);
                        thread::spawn(move || {
                            let _ = cas_store::server::serve_connection(stream, repo);
                        });
                    }
                    Err(e) if e.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        HttpServer { addr, shutdown }
    }
}

impl Drop for HttpServer {
    fn drop(&mut self) {
        self.shutdown
            .store(true, std::sync::atomic::Ordering::Relaxed);
    }
}

/// Minimal HTTP/1.1 client for tests.
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
    pub fn json_value(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body).unwrap_or(serde_json::Value::Null)
    }
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

pub fn request(
    addr: &str,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> HttpResponse {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(15))).unwrap();
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n");
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    if !body.is_empty() && !headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("content-length")) {
        req.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    stream.flush().unwrap();

    let mut raw = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(ref e) if e.kind() == std::io::ErrorKind::TimedOut => break,
            Err(e) => panic!("read error: {e}"),
        }
    }
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let head = String::from_utf8_lossy(&raw[..split]).to_string();
    let body = raw[split + 4..].to_vec();
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let headers = lines
        .filter_map(|l| l.split_once(':').map(|(k, v)| (k.to_string(), v.trim().to_string())))
        .collect();
    HttpResponse {
        status,
        headers,
        body,
    }
}

/// Suppress unused warning when `serve_listener` isn't referenced directly.
#[allow(dead_code)]
fn _use_serve(f: fn(Repository, TcpListener) -> std::io::Result<()>) {
    let _ = f;
}
