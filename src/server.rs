//! Minimal std-only HTTP/1.1 verification surface.
//!
//! This is intentionally not a general-purpose web framework: one connection
//! per request, no keep-alive, tiny routing table. It exists to exercise the
//! storage engine over a real local socket so acceptance checks can use plain
//! HTTP. All payloads use simple line protocols rather than pulling in a JSON
//! parser; responses are hand-emitted JSON.
//!
//! ## Routes
//!
//! | Method | Path                                   | Meaning |
//! |--------|----------------------------------------|---------|
//! | GET    | `/health`                              | liveness |
//! | GET    | `/series`                              | list series |
//! | POST   | `/series/{name}?policy=&block_size=`   | create series |
//! | GET    | `/series/{name}/stats`                 | compression stats |
//! | POST   | `/series/{name}/points?sync=`          | append `ts,value` lines |
//! | GET    | `/series/{name}/range?start=&end=`     | inclusive range query |
//! | POST   | `/series/{name}/flush`                 | fsync active block |
//! | POST   | `/series/{name}/drain`                 | merge buffered late points |
//! | GET    | `/series/{name}/buffered`              | count of late-buffered points |
//! | POST   | `/dev/fault`                           | inject I/O faults (line body) |
//!
//! Write body lines are `timestamp,value` (both signed i64), separated by
//! `\n`; blank lines are ignored. `sync=true` forces a flush+fsync before the
//! response is returned (default: flush automatically only at block size).
//!
//! Fault body lines: `write_limit=<bytes>`, `fail_syncs=<n>`, `clear`.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::coding::Point;
use crate::io_layer::{FaultRules, FaultyIo, RealIo};
use crate::store::{Store, StoreError, UnorderedPolicy};

/// Shared server state.
pub struct AppState {
    pub store: Store,
    pub fault_rules: Option<Arc<FaultRules>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultMode {
    Off,
    On,
}

/// Parse and bind a server. With [`FaultMode::On`] the store runs behind a
/// [`FaultyIo`] backend and `/dev/fault` is live; without it, the route
/// returns 404.
pub fn serve(
    addr: &str,
    data_dir: PathBuf,
    block_size: u32,
    fault_mode: FaultMode,
) -> std::io::Result<ServerHandle> {
    let listener = TcpListener::bind(addr)?;
    let local_addr = listener.local_addr()?;

    let state = match fault_mode {
        FaultMode::Off => {
            let store = Store::open(&data_dir, Arc::new(RealIo::new()), block_size)
                .map_err(std::io::Error::other)?;
            Arc::new(AppState {
                store,
                fault_rules: None,
            })
        }
        FaultMode::On => {
            let rules = Arc::new(FaultRules::new());
            let io = Arc::new(FaultyIo::new(Arc::new(RealIo::new()), Arc::clone(&rules)));
            let store = Store::open(&data_dir, io, block_size).map_err(std::io::Error::other)?;
            Arc::new(AppState {
                store,
                fault_rules: Some(rules),
            })
        }
    };

    let handle = ServerHandle {
        addr: local_addr,
        stop: Arc::new(std::sync::atomic::AtomicBool::new(false)),
    };
    let stop_flag = Arc::clone(&handle.stop);

    std::thread::spawn(move || {
        listener
            .set_nonblocking(true)
            .expect("set_nonblocking on listener");
        while !stop_flag.load(std::sync::atomic::Ordering::SeqCst) {
            match listener.accept() {
                Ok((stream, _peer)) => {
                    let state = Arc::clone(&state);
                    // Handle each connection on its own short-lived thread.
                    std::thread::spawn(move || {
                        let _ = handle_connection(stream, state);
                    });
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                Err(_) => break,
            }
        }
    });

    Ok(handle)
}

pub struct ServerHandle {
    addr: std::net::SocketAddr,
    stop: Arc<std::sync::atomic::AtomicBool>,
}

impl ServerHandle {
    pub fn addr(&self) -> std::net::SocketAddr {
        self.addr
    }

    pub fn port(&self) -> u16 {
        self.addr.port()
    }

    pub fn shutdown(&self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
    }
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

// ---------------------------------------------------------------------------
// Connection handling
// ---------------------------------------------------------------------------

struct Request {
    method: String,
    path: String, // path portion only
    query: String,
    body: String,
}

fn handle_connection(mut stream: TcpStream, state: Arc<AppState>) -> std::io::Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(std::time::Duration::from_secs(5)))?;

    // Read until end of headers, accumulating up to 1 MiB.
    let mut buf = Vec::with_capacity(2048);
    let mut byte = [0u8; 1];
    let mut header_end = None;
    while header_end.is_none() && buf.len() < 1024 * 1024 {
        match stream.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                buf.push(byte[0]);
                let terminator = (buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n")
                    || (buf.len() >= 2 && &buf[buf.len() - 2..] == b"\n\n");
                if terminator {
                    header_end = Some(buf.len());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(e),
        }
    }
    let header_end = match header_end {
        Some(n) => n,
        None => {
            return send_simple(&mut stream, 400, "malformed request (no header end)");
        }
    };

    let header_text = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = header_text.split("\r\n").filter(|l| !l.is_empty());
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("");
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target.to_string(), String::new()),
    };
    let content_length: usize = header_text
        .lines()
        .find_map(|line| {
            let lower = line.to_ascii_lowercase();
            lower
                .strip_prefix("content-length:")
                .and_then(|v| v.trim().parse().ok())
        })
        .unwrap_or(0);

    let mut body = String::new();
    if content_length > 0 {
        // Remaining bytes after the header terminator.
        let already = buf.len() - header_end;
        let mut rest = vec![0u8; content_length.saturating_sub(already)];
        stream.read_exact(&mut rest)?;
        let mut all = buf[header_end..].to_vec();
        all.extend_from_slice(&rest);
        all.truncate(content_length);
        body = String::from_utf8_lossy(&all).to_string();
    }

    let req = Request {
        method,
        path,
        query,
        body,
    };
    let (status, json) = route(&req, &state);
    send_response(&mut stream, status, &json)
}

fn send_simple(stream: &mut TcpStream, status: u16, msg: &str) -> std::io::Result<()> {
    let body = format!(
        "{{\"ok\":{},\"error\":{}}}",
        status == 200,
        json_string(msg)
    );
    send_response(stream, status, &body)
}

fn send_response(stream: &mut TcpStream, status: u16, body: &str) -> std::io::Result<()> {
    let reason = reason_phrase(status);
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

fn route(req: &Request, state: &AppState) -> (u16, String) {
    match try_route(req, state) {
        Ok((status, body)) => (status, body),
        Err((status, msg)) => (
            status,
            format!("{{\"ok\":false,\"error\":{}}}", json_string(&msg)),
        ),
    }
}

type RouteResult = Result<(u16, String), (u16, String)>;

fn err(status: u16, msg: impl Into<String>) -> RouteResult {
    Err((status, msg.into()))
}

fn try_route(req: &Request, state: &AppState) -> RouteResult {
    let path = req.path.trim_end_matches('/');

    if path == "/health" && req.method == "GET" {
        return Ok((
            200,
            format!(
                "{{\"ok\":true,\"time_ms\":{}}}",
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0)
            ),
        ));
    }

    if path == "/series" && req.method == "GET" {
        let names = state.store.list_series();
        let arr = names
            .iter()
            .map(|n| json_string(n))
            .collect::<Vec<_>>()
            .join(",");
        return Ok((200, format!("{{\"series\":[{arr}]}}")));
    }

    if path == "/dev/fault" && req.method == "POST" {
        let rules = state.fault_rules.as_ref().ok_or_else(|| {
            (
                404,
                "fault injection disabled (start server with --fault)".to_string(),
            )
        })?;
        for line in req.body.lines().map(str::trim).filter(|l| !l.is_empty()) {
            if line == "clear" {
                rules.clear();
            } else if let Some(v) = line.strip_prefix("write_limit=") {
                if v == "none" {
                    rules.clear();
                } else {
                    let limit: u64 = parse_i64(v)
                        .ok()
                        .and_then(|x| u64::try_from(x).ok())
                        .ok_or_else(|| (400, format!("bad write_limit: {v}")))?;
                    rules.fail_writes_after(limit);
                }
            } else if let Some(v) = line.strip_prefix("fail_syncs=") {
                let n: usize = v
                    .parse()
                    .map_err(|_| (400, format!("bad fail_syncs: {v}")))?;
                rules.fail_next_syncs(n);
            } else {
                return err(400, format!("unknown fault directive: {line}"));
            }
        }
        return Ok((
            200,
            format!(
                "{{\"ok\":true,\"bytes_written\":{}}}",
                rules.bytes_written()
            ),
        ));
    }

    // Series-scoped routes.
    if let Some(rest) = path.strip_prefix("/series/") {
        let segments: Vec<&str> = rest.split('/').collect();
        return match segments.as_slice() {
            [name] if req.method == "POST" => create_series(state, name, &req.query),
            [name, "stats"] if req.method == "GET" => series_stats(state, name),
            [name, "points"] if req.method == "POST" => {
                write_points(state, name, &req.query, &req.body)
            }
            [name, "range"] if req.method == "GET" => range_query(state, name, &req.query),
            [name, "flush"] if req.method == "POST" => flush_series(state, name),
            [name, "drain"] if req.method == "POST" => drain_series(state, name),
            [name, "buffered"] if req.method == "GET" => buffered_count(state, name),
            _ => err(404, format!("no such route: {} {}", req.method, req.path)),
        };
    }

    err(404, format!("no such route: {} {}", req.method, req.path))
}

fn create_series(state: &AppState, name: &str, query: &str) -> RouteResult {
    let params = parse_query(query);
    let policy = match qget(&params, "policy").unwrap_or("reject") {
        "reject" => UnorderedPolicy::Reject,
        "buffer" => UnorderedPolicy::Buffer,
        other => return err(400, format!("unknown policy: {other}")),
    };
    let block_size = match qget(&params, "block_size") {
        Some(v) => Some(
            v.parse::<u32>()
                .map_err(|_| (400, format!("bad block_size: {v}")))?
                .max(1),
        ),
        None => None,
    };
    state
        .store
        .create_series(name, policy, block_size)
        .map_err(store_err_status)?;
    Ok((
        201,
        format!(
            "{{\"ok\":true,\"series\":{},\"policy\":\"{}\"}}",
            json_string(name),
            if matches!(policy, UnorderedPolicy::Buffer) {
                "buffer"
            } else {
                "reject"
            }
        ),
    ))
}

fn series_stats(state: &AppState, name: &str) -> RouteResult {
    let s = state.store.stats(name).map_err(store_err_status)?;
    Ok((
        200,
        format!(
            "{{\"series\":{},\"policy\":\"{}\",\"block_size\":{},\"blocks\":{},\"flushed_points\":{},\"active_points\":{},\"buffered_points\":{},\"disk_bytes\":{},\"uncompressed_bytes\":{},\"compression_ratio\":{}}}",
            json_string(name),
            if matches!(s.policy, UnorderedPolicy::Buffer) { "buffer" } else { "reject" },
            s.block_size,
            s.blocks,
            s.flushed_points,
            s.active_points,
            s.buffered_points,
            s.disk_bytes,
            s.uncompressed_bytes,
            match s.compression_ratio {
                Some(r) => format!("{r:.6}"),
                None => "null".to_string(),
            }
        ),
    ))
}

fn write_points(state: &AppState, name: &str, query: &str, body: &str) -> RouteResult {
    let mut points = Vec::new();
    for (i, line) in body.lines().map(str::trim).enumerate() {
        if line.is_empty() {
            continue;
        }
        let (ts_s, v_s) = line
            .split_once(',')
            .ok_or_else(|| (400, format!("line {}: expected `ts,value`", i + 1)))?;
        let ts = parse_i64(ts_s.trim()).map_err(|_| (400, format!("line {}: bad ts", i + 1)))?;
        let value =
            parse_i64(v_s.trim()).map_err(|_| (400, format!("line {}: bad value", i + 1)))?;
        points.push(Point::new(ts, value));
    }
    if points.is_empty() {
        return err(400, "empty batch");
    }

    let outcome = state.store.write(name, &points).map_err(store_err_status)?;

    let sync = qget(&parse_query(query), "sync")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(false);
    let mut flushed = outcome.blocks_flushed;
    if sync {
        flushed += state.store.flush(Some(name)).map_err(store_err_status)?;
    }

    Ok((
        200,
        format!(
            "{{\"ok\":true,\"accepted\":{},\"buffered\":{},\"blocks_flushed\":{}}}",
            outcome.accepted, outcome.buffered, flushed
        ),
    ))
}

fn range_query(state: &AppState, name: &str, query: &str) -> RouteResult {
    let params = parse_query(query);
    let start = qget(&params, "start")
        .ok_or_else(|| (400, "missing `start`".to_string()))
        .and_then(|v| parse_i64(v).map_err(|_| (400, format!("bad start: {v}"))))?;
    let end = qget(&params, "end")
        .ok_or_else(|| (400, "missing `end`".to_string()))
        .and_then(|v| parse_i64(v).map_err(|_| (400, format!("bad end: {v}"))))?;
    if start > end {
        return err(400, "start > end");
    }

    let report = state
        .store
        .query(name, start, end)
        .map_err(store_err_status)?;
    let arr = report
        .points
        .iter()
        .map(|p| format!("[{},{}]", p.ts, p.value))
        .collect::<Vec<_>>()
        .join(",");
    Ok((
        200,
        format!(
            "{{\"series\":{},\"start\":{start},\"end\":{end},\"count\":{},\"blocks_scanned\":{},\"blocks_with_hits\":{},\"bytes_read\":{},\"points\":[{arr}]}}",
            json_string(name),
            report.points.len(),
            report.blocks_scanned,
            report.blocks_with_hits,
            report.bytes_read
        ),
    ))
}

fn flush_series(state: &AppState, name: &str) -> RouteResult {
    let n = state.store.flush(Some(name)).map_err(store_err_status)?;
    Ok((200, format!("{{\"ok\":true,\"blocks_flushed\":{n}}}")))
}

fn drain_series(state: &AppState, name: &str) -> RouteResult {
    let d = state.store.drain(name).map_err(store_err_status)?;
    Ok((
        200,
        format!(
            "{{\"ok\":true,\"merged\":{},\"blocks_rewritten\":{},\"buffered_remaining\":{}}}",
            d.merged, d.blocks_rewritten, d.buffered_remaining
        ),
    ))
}

fn buffered_count(state: &AppState, name: &str) -> RouteResult {
    let n = state.store.buffered_count(name).map_err(store_err_status)?;
    Ok((
        200,
        format!("{{\"series\":{},\"buffered\":{n}}}", json_string(name)),
    ))
}

fn store_err_status(e: StoreError) -> (u16, String) {
    let status = match &e {
        StoreError::UnknownSeries(_) => 404,
        StoreError::SeriesExists(_) | StoreError::OutOfOrder { .. } => 409,
        StoreError::BadSeriesName(_)
        | StoreError::NotAStore
        | StoreError::UnsupportedVersion(_)
        | StoreError::Codec(_) => 400,
        StoreError::Corruption { .. } => 500,
        StoreError::Io(ioe) => {
            use std::io::ErrorKind::*;
            match ioe.kind() {
                NotFound => 404,
                PermissionDenied => 403,
                _ => 500,
            }
        }
    };
    (status, e.to_string())
}

// ---------------------------------------------------------------------------
// Small parsing/formatting helpers
// ---------------------------------------------------------------------------

fn parse_query(query: &str) -> Vec<(&str, &str)> {
    query
        .split('&')
        .filter(|p| !p.is_empty())
        .filter_map(|p| p.split_once('='))
        .collect()
}

/// First value for a query parameter.
fn qget<'a>(params: &'a [(&str, &str)], key: &str) -> Option<&'a str> {
    params.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
}

/// Strict signed i64 decimal parse (optional leading `-`, digits only,
/// range-checked). Rejects `+`, whitespace, underscores and overflow.
fn parse_i64(s: &str) -> Result<i64, ()> {
    if s.is_empty() {
        return Err(());
    }
    let (neg, digits) = match s.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, s),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(());
    }
    let mut value: i128 = 0;
    for b in digits.bytes() {
        value = value * 10 + (b - b'0') as i128;
        if value > i64::MAX as i128 + neg as i128 {
            return Err(());
        }
    }
    // Stay in i128 so -2^63 (whose magnitude 2^63 does not fit i64) is fine.
    Ok(if neg { (-value) as i64 } else { value as i64 })
}

/// Minimal JSON string escaper for the characters that must be escaped.
fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Convenience for integration tests: perform one request over a real socket.
pub fn raw_request(addr: std::net::SocketAddr, raw: &str) -> (u16, String) {
    use std::io::Read as _;
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.write_all(raw.as_bytes()).unwrap();
    let mut data = Vec::new();
    stream.read_to_end(&mut data).unwrap();
    let text = String::from_utf8_lossy(&data);
    let status = text
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = text.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    (status, body)
}

/// JSON-body POST helper for tests / scripts.
pub fn post(addr: std::net::SocketAddr, path: &str, body: &str) -> (u16, String) {
    let raw = format!(
        "POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    raw_request(addr, &raw)
}

pub fn get(addr: std::net::SocketAddr, path: &str) -> (u16, String) {
    let raw = format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    raw_request(addr, &raw)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_i64_strict() {
        assert_eq!(parse_i64("0"), Ok(0));
        assert_eq!(parse_i64("-1"), Ok(-1));
        assert_eq!(parse_i64("9223372036854775807"), Ok(i64::MAX));
        assert_eq!(parse_i64("-9223372036854775808"), Ok(i64::MIN));
        assert!(parse_i64("9223372036854775808").is_err());
        assert!(parse_i64("-9223372036854775809").is_err());
        assert!(parse_i64("").is_err());
        assert!(parse_i64("1.0").is_err());
        assert!(parse_i64("+1").is_err());
        assert!(parse_i64(" 1").is_err());
    }

    #[test]
    fn json_escape() {
        assert_eq!(json_string("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(json_string("x\ny"), "\"x\\ny\"");
    }
}
