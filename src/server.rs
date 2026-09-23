//! Minimal HTTP/1.1 server over `std::net` (no dependencies).
//!
//! One thread per connection, `Connection: close`, JSON in/out. All engine
//! calls are made through the shared [`Engine`] handle; the engine's own
//! mutex serializes state transitions, so this server is safe to hammer
//! from concurrent clients.
//!
//! Endpoints (all JSON):
//!
//! | Method & path              | Body                        | Meaning |
//! |----------------------------|-----------------------------|---------|
//! | GET  /health               | —                           | liveness |
//! | GET  /stats                | —                           | engine counters |
//! | POST /tx/begin             | —                           | begin tx |
//! | POST /tx/{id}/put          | {"key":"k","value":"v"}     | buffer write |
//! | POST /tx/{id}/delete       | {"key":"k"}                 | buffer delete |
//! | POST /tx/{id}/get          | {"key":"k"}                 | read in tx |
//! | POST /tx/{id}/commit       | —                           | commit (409 on conflict) |
//! | POST /tx/{id}/abort        | —                           | abort |
//! | POST /snapshot             | —                           | pin a snapshot |
//! | POST /snapshot/{id}/get    | {"key":"k"}                 | read pinned snapshot |
//! | POST /snapshot/{id}/release| —                           | release pin |
//! | POST /gc                   | —                           | run reclamation |
//! | GET  /admin/faults         | —                           | armed fault |
//! | POST /admin/faults         | {"op":"sync","leave_written":false} | arm one-shot fault |
//! | POST /admin/faults/reset   | —                           | disarm |

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::thread;

use crate::engine::{Engine, GcReport, Stats};
use crate::error::Error;
use crate::json::Json;
use crate::vfs::FaultOp;

pub struct Server {
    engine: Engine,
    addr: String,
}

impl Server {
    pub fn new(engine: Engine, addr: impl Into<String>) -> Self {
        Server {
            engine,
            addr: addr.into(),
        }
    }

    /// Bind and serve forever (blocking).
    pub fn serve(self) -> io::Result<()> {
        let listener = TcpListener::bind(&self.addr)?;
        let engine = Arc::new(self.engine);
        eprintln!("mvcc-gc listening on http://{}", listener.local_addr()?);
        for stream in listener.incoming() {
            match stream {
                Ok(s) => {
                    let engine = engine.clone();
                    thread::spawn(move || {
                        let _ = handle_conn(s, engine);
                    });
                }
                Err(e) => eprintln!("accept error: {e}"),
            }
        }
        Ok(())
    }

    /// Route one request. Exposed for tests.
    pub fn handle(&self, method: &str, path: &str, body: &[u8]) -> (u16, Json) {
        route(&self.engine, method, path, body)
    }
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

fn route(engine: &Engine, method: &str, path: &str, body: &[u8]) -> (u16, Json) {
    let result: crate::error::Result<Json> = (|| match (method, path) {
        ("GET", "/health") => Ok(obj_with("status", Json::string("ok"))),
        ("GET", "/stats") => Ok(stats_json(&engine.stats())),
        ("POST", "/tx/begin") => {
            let (id, read_version) = engine.begin();
            let mut o = Json::obj();
            o.insert("tx", Json::uint(id));
            o.insert("read_version", Json::uint(read_version));
            Ok(o)
        }
        ("POST", "/snapshot") => {
            let s = engine.snapshot();
            let mut o = Json::obj();
            o.insert("snapshot", Json::uint(s.id));
            o.insert("version", Json::uint(s.version));
            Ok(o)
        }
        ("POST", "/gc") => Ok(gc_json(engine.gc()?)),
        ("GET", "/admin/faults") => {
            let mut o = Json::obj();
            match engine.faults().armed() {
                Some(op) => o.insert("armed", Json::string(op.as_str())),
                None => o.insert("armed", Json::Null),
            }
            Ok(o)
        }
        ("POST", "/admin/faults") => {
            let j = parse_body(body)?;
            let op = j
                .get("op")
                .and_then(Json::as_str)
                .and_then(FaultOp::parse)
                .ok_or_else(|| {
                    Error::BadRequest(
                        "op must be one of write|sync|rename|sync_parent|remove".into(),
                    )
                })?;
            let leave = j
                .get("leave_written")
                .and_then(Json::as_bool)
                .unwrap_or(false);
            engine.faults().arm(op, leave);
            let mut o = Json::obj();
            o.insert("armed", Json::string(op.as_str()));
            o.insert("leave_written", Json::Bool(leave));
            Ok(o)
        }
        ("POST", "/admin/faults/reset") => {
            engine.faults().disarm();
            Ok(obj_with("disarmed", Json::Bool(true)))
        }
        _ => route_dynamic(engine, method, path, body),
    })();

    match result {
        Ok(mut j) => {
            j.insert("ok", Json::Bool(true));
            (200, j)
        }
        Err(e) => {
            let status = match &e {
                Error::NotFound | Error::UnknownTx(_) | Error::UnknownSnapshot(_) => 404,
                Error::Conflict(_) => 409,
                Error::BadRequest(_) => 400,
                _ => 500,
            };
            let mut j = Json::obj();
            j.insert("ok", Json::Bool(false));
            j.insert("error", Json::string(e.to_string()));
            (status, j)
        }
    }
}

fn route_dynamic(
    engine: &Engine,
    method: &str,
    path: &str,
    body: &[u8],
) -> crate::error::Result<Json> {
    // /tx/{id}/{action}
    if let Some(rest) = path.strip_prefix("/tx/") {
        let (id, action) = split_id(rest)?;
        match (method, action) {
            ("POST", "put") => {
                let j = parse_body(body)?;
                let key = body_key(&j)?;
                let value = j
                    .get("value")
                    .and_then(Json::as_str)
                    .ok_or_else(|| Error::BadRequest("missing string field \"value\"".into()))?;
                engine.put(id, key.as_bytes().to_vec(), value.as_bytes().to_vec())?;
                return Ok(obj_with("buffered", Json::Bool(true)));
            }
            ("POST", "delete") => {
                let j = parse_body(body)?;
                engine.delete(id, body_key(&j)?.as_bytes().to_vec())?;
                return Ok(obj_with("buffered", Json::Bool(true)));
            }
            ("POST", "get") => {
                let j = parse_body(body)?;
                let key = body_key(&j)?;
                return Ok(value_json(engine.get_tx(id, key.as_bytes())?, key));
            }
            ("POST", "commit") => {
                let version = engine.commit(id)?;
                return Ok(obj_with("version", Json::uint(version)));
            }
            ("POST", "abort") => {
                engine.abort(id)?;
                return Ok(obj_with("aborted", Json::Bool(true)));
            }
            _ => {}
        }
    }

    // /snapshot/{id}/{action}
    if let Some(rest) = path.strip_prefix("/snapshot/") {
        let (id, action) = split_id(rest)?;
        match (method, action) {
            ("POST", "get") => {
                let j = parse_body(body)?;
                let key = body_key(&j)?;
                return Ok(value_json(engine.get_snapshot(id, key.as_bytes())?, key));
            }
            ("POST", "release") => {
                engine.release_snapshot(id)?;
                return Ok(obj_with("released", Json::Bool(true)));
            }
            _ => {}
        }
    }

    Err(Error::BadRequest(format!("no route for {method} {path}")))
}

fn split_id(rest: &str) -> crate::error::Result<(u64, &str)> {
    let (id_s, action) = rest
        .split_once('/')
        .ok_or_else(|| Error::BadRequest("expected /{id}/{action}".into()))?;
    let id: u64 = id_s
        .parse()
        .map_err(|_| Error::BadRequest(format!("bad id {id_s:?}")))?;
    Ok((id, action))
}

fn parse_body(body: &[u8]) -> crate::error::Result<Json> {
    if body.is_empty() {
        return Ok(Json::obj());
    }
    let s = std::str::from_utf8(body).map_err(|_| Error::BadRequest("body is not utf-8".into()))?;
    Json::parse(s).map_err(Error::BadRequest)
}

fn body_key(j: &Json) -> crate::error::Result<&str> {
    j.get("key")
        .and_then(Json::as_str)
        .ok_or_else(|| Error::BadRequest("missing string field \"key\"".into()))
}

fn value_json(found: Option<Vec<u8>>, key: &str) -> Json {
    let mut o = Json::obj();
    o.insert("key", Json::string(key));
    match found {
        Some(v) => {
            o.insert("found", Json::Bool(true));
            o.insert(
                "value",
                Json::string(String::from_utf8_lossy(&v).into_owned()),
            );
        }
        None => {
            o.insert("found", Json::Bool(false));
        }
    }
    o
}

fn obj_with(k: &str, v: Json) -> Json {
    let mut o = Json::obj();
    o.insert(k, v);
    o
}

fn stats_json(s: &Stats) -> Json {
    let mut o = Json::obj();
    o.insert("current_version", Json::uint(s.current_version));
    o.insert("base_version", Json::uint(s.base_version));
    o.insert("keys", Json::uint(s.keys as u64));
    o.insert("live_cells", Json::uint(s.live_cells as u64));
    o.insert(
        "open_transactions",
        Json::Arr(
            s.open_transactions
                .iter()
                .map(|(id, rv)| {
                    let mut t = Json::obj();
                    t.insert("tx", Json::uint(*id));
                    t.insert("read_version", Json::uint(*rv));
                    t
                })
                .collect(),
        ),
    );
    o.insert(
        "snapshots",
        Json::Arr(
            s.snapshots
                .iter()
                .map(|s| {
                    let mut t = Json::obj();
                    t.insert("snapshot", Json::uint(s.id));
                    t.insert("version", Json::uint(s.version));
                    t
                })
                .collect(),
        ),
    );
    let mut files = Json::obj();
    files.insert("segments", Json::uint(s.segment_files as u64));
    files.insert("bases", Json::uint(s.base_files as u64));
    files.insert("stale", Json::uint(s.stale_files as u64));
    files.insert("live_bytes", Json::uint(s.live_bytes));
    files.insert("stale_bytes", Json::uint(s.stale_bytes));
    o.insert("files", files);
    o
}

fn gc_json(r: GcReport) -> Json {
    let mut o = Json::obj();
    o.insert("horizon", Json::uint(r.horizon));
    o.insert("base_version_before", Json::uint(r.base_version_before));
    o.insert("base_written", Json::Bool(r.base_written));
    o.insert("base_cells", Json::uint(r.base_cells as u64));
    o.insert("files_removed", Json::uint(r.files_removed as u64));
    o.insert("bytes_reclaimed", Json::uint(r.bytes_reclaimed));
    o.insert("stale_left", Json::uint(r.stale_left as u64));
    match r.noop_reason {
        Some(reason) => o.insert("noop_reason", Json::string(reason)),
        None => o.insert("noop_reason", Json::Null),
    }
    o
}

// ---------------------------------------------------------------------------
// HTTP wire
// ---------------------------------------------------------------------------

const MAX_HEAD: usize = 64 * 1024;
const MAX_BODY: usize = 16 * 1024 * 1024;

fn handle_conn(mut stream: TcpStream, engine: Arc<Engine>) -> io::Result<()> {
    let mut buf = Vec::with_capacity(4096);
    let mut chunk = [0u8; 8192];

    // Read headers.
    let head_end = loop {
        if let Some(pos) = find_subslice(&buf, b"\r\n\r\n") {
            break pos + 4;
        }
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            return Ok(());
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > MAX_HEAD {
            return write_response(
                &mut stream,
                431,
                b"{\"ok\":false,\"error\":\"head too large\"}",
            );
        }
    };

    let head = String::from_utf8_lossy(&buf[..head_end]).into_owned();
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();

    let mut content_length = 0usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if content_length > MAX_BODY {
        return write_response(
            &mut stream,
            413,
            b"{\"ok\":false,\"error\":\"body too large\"}",
        );
    }

    // Read the rest of the body.
    let mut body = buf.split_off(head_end);
    while body.len() < content_length {
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&chunk[..n]);
    }
    body.truncate(content_length);

    let server = Server {
        engine: (*engine).clone(),
        addr: String::new(),
    };
    let (status, json) = server.handle(&method, &path, &body);
    let payload = json.to_bytes();
    write_response(&mut stream, status, &payload)
}

fn write_response(stream: &mut TcpStream, status: u16, payload: &[u8]) -> io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        _ => "Internal Server Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        payload.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}
