//! Minimal dependency-free HTTP/1.1 verification entry point.
//!
//! This server exists to exercise the store over real sockets; it is not a
//! production web server (thread per connection, no TLS, no auth).
//!
//! Routes
//! ------
//! ```text
//! GET  /health
//! POST /blocks                      raw body  OR  application/json {"data_b64": "...", "hash": "...", "refs": ["..."]}
//! GET  /blocks/{hash}               raw bytes (digest re-verified)
//! GET  /blocks/{hash}/info          metadata json
//! PUT  /roots/{name}                raw hash in body OR json {"hash": "..."}
//!                                    header If-Match: version N for OCC
//! GET  /roots                       list
//! GET  /roots/{name}                json
//! DELETE /roots/{name}
//! POST /gc                          run mark-sweep, report json
//! GET  /stats
//! ```

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::hash::Sha256;
use crate::json::Json;
use crate::store::{DynStore, StoreError};

pub struct Server {
    listener: TcpListener,
    store: Arc<DynStore>,
    stop: Arc<AtomicBool>,
}

#[derive(Debug)]
pub struct ServerConfig {
    pub read_timeout: Duration,
    pub max_body_bytes: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            read_timeout: Duration::from_secs(30),
            max_body_bytes: crate::store::DEFAULT_MAX_BLOCK_BYTES,
        }
    }
}

impl Server {
    /// Bind immediately so the chosen port is known before `serve` runs.
    pub fn bind(addr: &str, store: Arc<DynStore>) -> std::io::Result<Self> {
        let listener = TcpListener::bind(addr)?;
        listener.set_nonblocking(true)?;
        Ok(Server {
            listener,
            store,
            stop: Arc::new(AtomicBool::new(false)),
        })
    }

    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }

    pub fn shutdown_handle(&self) -> Arc<AtomicBool> {
        self.stop.clone()
    }

    /// Accept connections until `shutdown()` is called.
    pub fn serve(self, cfg: ServerConfig) {
        while !self.stop.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, peer)) => {
                    let store = self.store.clone();
                    let stop = self.stop.clone();
                    let cfg = ServerConfig { ..cfg };
                    thread::spawn(move || {
                        let _ = handle_connection(stream, peer, store, &stop, cfg);
                    });
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => break,
            }
        }
    }

    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

// ---------------- per-connection handling ----------------

#[derive(Debug)]
struct Request {
    method: String,
    path: String,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}

fn handle_connection(
    mut stream: TcpStream,
    _peer: std::net::SocketAddr,
    store: Arc<DynStore>,
    stop: &AtomicBool,
    cfg: ServerConfig,
) -> std::io::Result<()> {
    stream.set_read_timeout(Some(cfg.read_timeout))?;
    stream.set_write_timeout(Some(Duration::from_secs(30)))?;
    let _ = stream.set_nodelay(true);

    loop {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        let req = match read_request(&mut stream, cfg.max_body_bytes) {
            Ok(Some(r)) => r,
            Ok(None) => return Ok(()), // clean connection close
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => return Ok(()),
            Err(_) => return Ok(()),
        };
        let keep_alive = req
            .headers
            .get("connection")
            .map(|v| !v.eq_ignore_ascii_case("close"))
            .unwrap_or(true);

        let resp = route(&req, &store, &cfg);
        write_response(&mut stream, &resp)?;
        if !keep_alive {
            return Ok(());
        }
    }
}

fn read_request(stream: &mut TcpStream, max_body: usize) -> std::io::Result<Option<Request>> {
    let mut header_buf = Vec::new();
    let mut byte = [0u8; 1];
    loop {
        match stream.read(&mut byte) {
            Ok(0) if header_buf.is_empty() => return Ok(None),
            Ok(0) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "eof",
                ))
            }
            Ok(_) => {
                header_buf.push(byte[0]);
                if header_buf.len() >= 4 && &header_buf[header_buf.len() - 4..] == b"\r\n\r\n" {
                    break;
                }
                if header_buf.len() > 64 * 1024 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "headers too large",
                    ));
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }

    let text = String::from_utf8_lossy(&header_buf);
    let mut lines = text.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.splitn(3, ' ');
    let method = parts.next().unwrap_or("").to_string();
    let raw_target = parts.next().unwrap_or("");
    if method.is_empty() || raw_target.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "bad request line",
        ));
    }
    let (path, query) = match raw_target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (raw_target.to_string(), String::new()),
    };

    let mut headers = BTreeMap::new();
    for line in lines {
        if line.is_empty() {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }

    let mut body = Vec::new();
    if let Some(len_s) = headers.get("content-length") {
        let len: usize = len_s
            .parse()
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "bad length"))?;
        if len > max_body {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "body too large",
            ));
        }
        body.resize(len, 0);
        stream.read_exact(&mut body)?;
    }

    let _ = query; // parsed routes currently use path only
    Ok(Some(Request {
        method,
        path,
        headers,
        body,
    }))
}

// ---------------- responses ----------------

struct Response {
    status: u16,
    reason: &'static str,
    content_type: &'static str,
    body: Vec<u8>,
    extra_headers: Vec<(String, String)>,
}

impl Response {
    fn json(status: u16, reason: &'static str, value: &Json) -> Self {
        Response {
            status,
            reason,
            content_type: "application/json; charset=utf-8",
            body: value.to_compact_string().into_bytes(),
            extra_headers: Vec::new(),
        }
    }

    fn error(status: u16, reason: &'static str, code: &str, message: &str) -> Self {
        let mut obj = BTreeMap::new();
        obj.insert("error".into(), Json::str(code));
        obj.insert("message".into(), Json::str(message));
        Self::json(status, reason, &Json::Obj(obj))
    }

    fn bytes(status: u16, reason: &'static str, body: Vec<u8>, content_type: &'static str) -> Self {
        Response {
            status,
            reason,
            content_type,
            body,
            extra_headers: Vec::new(),
        }
    }
}

fn write_response(stream: &mut TcpStream, resp: &Response) -> std::io::Result<()> {
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nContent-Length: {}\r\nContent-Type: {}\r\nConnection: keep-alive\r\n",
        resp.status,
        resp.reason,
        resp.body.len(),
        resp.content_type
    );
    for (k, v) in &resp.extra_headers {
        head.push_str(k);
        head.push_str(": ");
        head.push_str(v);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    stream.write_all(&resp.body)?;
    stream.flush()
}

// ---------------- routing ----------------

fn route(req: &Request, store: &Arc<DynStore>, cfg: &ServerConfig) -> Response {
    let path = req.path.as_str();
    let method = req.method.as_str();

    match (method, path) {
        ("GET", "/health") => {
            let mut obj = BTreeMap::new();
            obj.insert("status".into(), Json::str("ok"));
            Response::json(200, "OK", &Json::Obj(obj))
        }
        ("GET", "/stats") => match store.stats() {
            Ok(s) => {
                let mut obj = BTreeMap::new();
                obj.insert("roots".into(), Json::from_u64(s.roots as u64));
                obj.insert("blocks".into(), Json::from_u64(s.blocks as u64));
                obj.insert("block_bytes".into(), Json::from_u64(s.block_bytes));
                Response::json(200, "OK", &Json::Obj(obj))
            }
            Err(e) => store_err(e),
        },
        ("POST", "/gc") => match store.gc() {
            Ok(rep) => Response::json(200, "OK", &gc_report_json(&rep)),
            Err(e) => store_err(e),
        },
        ("POST", "/blocks") => put_block_handler(req, store, cfg),
        ("GET", p) if p.starts_with("/blocks/") => {
            let rest = &p["/blocks/".len()..];
            if let Some(hash_hex) = rest.strip_suffix("/info") {
                match crate::store::parse_hash(hash_hex) {
                    Ok(h) => match store.block_info(&h) {
                        Ok(v) => Response::json(200, "OK", &v),
                        Err(e) => store_err(e),
                    },
                    Err(e) => store_err(e),
                }
            } else {
                match crate::store::parse_hash(rest) {
                    Ok(h) => match store.get_block(&h) {
                        Ok(data) => {
                            let mut r =
                                Response::bytes(200, "OK", data, "application/octet-stream");
                            r.extra_headers
                                .push(("X-Content-Sha256".into(), h.to_hex()));
                            r
                        }
                        Err(e) => store_err(e),
                    },
                    Err(e) => store_err(e),
                }
            }
        }
        ("GET", "/roots") => match store.list_roots() {
            Ok(roots) => {
                let arr = roots
                    .into_iter()
                    .map(|(name, rec)| {
                        let mut o = BTreeMap::new();
                        o.insert("name".into(), Json::str(name));
                        o.insert("hash".into(), Json::str(rec.hash.to_hex()));
                        o.insert("version".into(), Json::from_u64(rec.version));
                        o.insert("updated_at_ms".into(), Json::from_u64(rec.updated_at_ms));
                        Json::Obj(o)
                    })
                    .collect();
                Response::json(200, "OK", &Json::Arr(arr))
            }
            Err(e) => store_err(e),
        },
        ("PUT", p) if p.starts_with("/roots/") => {
            let name = &p["/roots/".len()..];
            put_root_handler(req, store, name)
        }
        ("GET", p) if p.starts_with("/roots/") => {
            let name = &p["/roots/".len()..];
            match store.get_root(name) {
                Ok(rec) => {
                    let mut o = BTreeMap::new();
                    o.insert("name".into(), Json::str(name));
                    o.insert("hash".into(), Json::str(rec.hash.to_hex()));
                    o.insert("version".into(), Json::from_u64(rec.version));
                    o.insert("updated_at_ms".into(), Json::from_u64(rec.updated_at_ms));
                    Response::json(200, "OK", &Json::Obj(o))
                }
                Err(e) => store_err(e),
            }
        }
        ("DELETE", p) if p.starts_with("/roots/") => {
            let name = &p["/roots/".len()..];
            match store.delete_root(name) {
                Ok(true) => {
                    let mut o = BTreeMap::new();
                    o.insert("deleted".into(), Json::Bool(true));
                    Response::json(200, "OK", &Json::Obj(o))
                }
                Ok(false) => Response::error(404, "Not Found", "root_not_found", name),
                Err(e) => store_err(e),
            }
        }
        _ => Response::error(404, "Not Found", "not_found", path),
    }
}

fn put_block_handler(req: &Request, store: &Arc<DynStore>, cfg: &ServerConfig) -> Response {
    let ctype = req
        .headers
        .get("content-type")
        .map(|s| s.as_str())
        .unwrap_or("application/octet-stream");

    let (data, declared, refs) = if ctype.contains("application/json") {
        let doc = match std::str::from_utf8(&req.body) {
            Ok(t) => match crate::json::parse(t) {
                Ok(j) => j,
                Err(e) => return Response::error(400, "Bad Request", "bad_json", &e.to_string()),
            },
            Err(_) => return Response::error(400, "Bad Request", "bad_json", "body is not utf-8"),
        };
        let b64 = match doc.get("data_b64").and_then(Json::as_str) {
            Some(s) => s,
            None => {
                return Response::error(
                    400,
                    "Bad Request",
                    "missing_data_b64",
                    "json body requires \"data_b64\"",
                )
            }
        };
        let data = match base64_decode(b64) {
            Some(d) => d,
            None => return Response::error(400, "Bad Request", "bad_base64", "invalid data_b64"),
        };
        if data.len() > cfg.max_body_bytes {
            return Response::error(413, "Payload Too Large", "too_large", "block too large");
        }
        let declared = match doc.get("hash") {
            Some(Json::Str(s)) => match Sha256::from_hex(s) {
                Some(h) => Some(h),
                None => return Response::error(400, "Bad Request", "bad_hash", "invalid hash hex"),
            },
            Some(Json::Null) | None => None,
            Some(_) => {
                return Response::error(400, "Bad Request", "bad_hash", "\"hash\" must be string")
            }
        };
        let mut refs = Vec::new();
        if let Some(arr) = doc.get("refs").and_then(Json::as_array) {
            for item in arr {
                match item.as_str().and_then(Sha256::from_hex) {
                    Some(h) => refs.push(h),
                    None => {
                        return Response::error(
                            400,
                            "Bad Request",
                            "bad_ref",
                            "refs entries must be sha256 hex strings",
                        )
                    }
                }
            }
        }
        (data, declared, refs)
    } else {
        // Raw upload; optional declared digest header.
        let declared = req
            .headers
            .get("x-content-sha256")
            .and_then(|s| Sha256::from_hex(s));
        (req.body.clone(), declared, Vec::new())
    };

    match store.put_block(&data, declared, &refs) {
        Ok(out) => {
            let mut o = BTreeMap::new();
            o.insert("hash".into(), Json::str(out.hash.to_hex()));
            o.insert("deduplicated".into(), Json::Bool(out.deduplicated));
            o.insert("refs_count".into(), Json::from_u64(out.refs_count as u64));
            o.insert("size".into(), Json::from_u64(data.len() as u64));
            Response::json(200, "OK", &Json::Obj(o))
        }
        Err(e) => store_err(e),
    }
}

fn put_root_handler(req: &Request, store: &Arc<DynStore>, name: &str) -> Response {
    let hash = if req
        .headers
        .get("content-type")
        .map(|c| c.contains("application/json"))
        .unwrap_or(false)
    {
        match std::str::from_utf8(&req.body)
            .ok()
            .and_then(|t| crate::json::parse(t).ok())
        {
            Some(doc) => match doc.get("hash").and_then(Json::as_str) {
                Some(s) => s.to_string(),
                None => {
                    return Response::error(400, "Bad Request", "missing_hash", "json needs hash")
                }
            },
            None => return Response::error(400, "Bad Request", "bad_json", "invalid json body"),
        }
    } else {
        String::from_utf8_lossy(&req.body).trim().to_string()
    };
    let hash = match Sha256::from_hex(&hash) {
        Some(h) => h,
        None => return Response::error(400, "Bad Request", "bad_hash", "invalid sha256 hex"),
    };
    let expected = req.headers.get("if-match").map(|v| v.trim().parse::<u64>());
    let expected = match expected {
        Some(Ok(n)) => Some(n),
        Some(Err(_)) => {
            return Response::error(
                400,
                "Bad Request",
                "bad_if_match",
                "If-Match must be integer",
            )
        }
        None => None,
    };
    match store.put_root(name, hash, expected) {
        Ok(rec) => {
            let mut o = BTreeMap::new();
            o.insert("name".into(), Json::str(name));
            o.insert("hash".into(), Json::str(rec.hash.to_hex()));
            o.insert("version".into(), Json::from_u64(rec.version));
            o.insert("updated_at_ms".into(), Json::from_u64(rec.updated_at_ms));
            Response::json(200, "OK", &Json::Obj(o))
        }
        Err(e) => store_err(e),
    }
}

fn gc_report_json(rep: &crate::store::GcReport) -> Json {
    let mut o = BTreeMap::new();
    o.insert(
        "roots_scanned".into(),
        Json::from_u64(rep.roots_scanned as u64),
    );
    o.insert(
        "blocks_before".into(),
        Json::from_u64(rep.blocks_before as u64),
    );
    o.insert("blocks_live".into(), Json::from_u64(rep.blocks_live as u64));
    o.insert(
        "blocks_removed".into(),
        Json::from_u64(rep.blocks_removed as u64),
    );
    o.insert("bytes_removed".into(), Json::from_u64(rep.bytes_removed));
    o.insert(
        "removed".into(),
        Json::Arr(rep.removed.iter().map(Json::str).collect()),
    );
    o.insert(
        "missing_references".into(),
        Json::Arr(
            rep.missing_references
                .iter()
                .map(|m| {
                    let mut mm = BTreeMap::new();
                    mm.insert(
                        "parent".into(),
                        m.parent.as_ref().map(Json::str).unwrap_or(Json::Null),
                    );
                    mm.insert("missing".into(), Json::str(m.missing.clone()));
                    Json::Obj(mm)
                })
                .collect(),
        ),
    );
    o.insert(
        "corrupt_blocks".into(),
        Json::Arr(
            rep.corrupt_blocks
                .iter()
                .map(|s| Json::str(s.clone()))
                .collect(),
        ),
    );
    o.insert(
        "warnings".into(),
        Json::Arr(rep.warnings.iter().map(|s| Json::str(s.clone())).collect()),
    );
    Json::Obj(o)
}

fn store_err(e: StoreError) -> Response {
    match e {
        StoreError::NotFound(m) => Response::error(404, "Not Found", "not_found", &m),
        StoreError::HashMismatch { declared, actual } => Response::error(
            422,
            "Unprocessable Entity",
            "hash_mismatch",
            &format!("declared {declared}, actual {actual}"),
        ),
        StoreError::Corrupt { hash, detail } => Response::error(
            409,
            "Conflict",
            "corrupt_block",
            &format!("{hash}: {detail}"),
        ),
        StoreError::InvalidRootName(n) => {
            Response::error(400, "Bad Request", "invalid_root_name", &n)
        }
        StoreError::InvalidHash(h) => Response::error(400, "Bad Request", "invalid_hash", &h),
        StoreError::VersionConflict {
            name,
            expected,
            actual,
        } => Response::error(
            409,
            "Conflict",
            "version_conflict",
            &format!("root {name}: expected {expected}, actual {actual}"),
        ),
        StoreError::TooLarge { size, limit } => Response::error(
            413,
            "Payload Too Large",
            "too_large",
            &format!("{size} > {limit}"),
        ),
        StoreError::BadMetadata { context, detail } => Response::error(
            500,
            "Internal Server Error",
            "bad_metadata",
            &format!("{context}: {detail}"),
        ),
        StoreError::Vfs(ve) => match ve {
            crate::vfs::VfsError::Injected(s) => Response::error(
                500,
                "Internal Server Error",
                "injected_fault",
                &format!("simulated I/O failure at {s}"),
            ),
            crate::vfs::VfsError::Io(ioe) => {
                Response::error(500, "Internal Server Error", "io_error", &ioe.to_string())
            }
        },
    }
}

// ---------------- base64 (standard alphabet, padding required) ----------------

fn base64_decode(input: &str) -> Option<Vec<u8>> {
    let bytes = input.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let pad = bytes.iter().rev().take_while(|&&b| b == b'=').count();
    if pad > 2 {
        return None;
    }
    let data_len = bytes.len() - pad;
    // '=' may only appear as the trailing padding.
    if bytes[..data_len].contains(&b'=') {
        return None;
    }
    let val = |b: u8| -> Option<u32> {
        match b {
            b'A'..=b'Z' => Some((b - b'A') as u32),
            b'a'..=b'z' => Some((b - b'a' + 26) as u32),
            b'0'..=b'9' => Some((b - b'0' + 52) as u32),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    };
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    let mut i = 0;
    while i < data_len {
        let take = (data_len - i).min(4);
        let mut accum = 0u32;
        for j in 0..take {
            accum = (accum << 6) | val(bytes[i + j])?;
        }
        // Align partial final groups as if padded with zero sextets.
        accum <<= (4 - take) * 6;
        out.push((accum >> 16) as u8);
        if take >= 3 {
            out.push((accum >> 8) as u8);
        }
        if take == 4 {
            out.push(accum as u8);
        }
        i += take;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_known() {
        assert_eq!(base64_decode("").unwrap(), b"");
        assert_eq!(base64_decode("Zg==").unwrap(), b"f");
        assert_eq!(base64_decode("Zm8=").unwrap(), b"fo");
        assert_eq!(base64_decode("Zm9v").unwrap(), b"foo");
        assert_eq!(base64_decode("aGVsbG8gd29ybGQ=").unwrap(), b"hello world");
        assert!(base64_decode("Zg=").is_none());
        assert!(base64_decode("====").is_none());
    }
}
