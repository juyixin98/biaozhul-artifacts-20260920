//! Minimal local HTTP server (validation entry point, no frontend).
//!
//! Endpoints:
//!   GET  /health
//!   POST /ingest   body: {"series":"cpu","points":[[ts,val],...]}
//!   POST /flush    body: {"series":"cpu"}        (or ?series=cpu)
//!   GET  /query?series=cpu&from=0&to=100
//!   GET  /stats?series=cpu
//!
//! Responses are JSON. Errors return 4xx/5xx with {"error": "..."}.

use crate::io::FileIO;
use crate::json::{self, Json};
use crate::storage::Repo;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;

const MAX_HEADER_BYTES: usize = 64 * 1024;
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

pub fn serve<IO>(repo: Arc<Mutex<Repo<IO>>>, addr: &str) -> io::Result<()>
where
    IO: FileIO + Send + 'static,
{
    let listener = TcpListener::bind(addr)?;
    eprintln!("tsblock listening on http://{}", listener.local_addr()?);
    serve_listener(repo, listener)
}

/// Serves on an already-bound listener (useful for tests binding port 0).
pub fn serve_listener<IO>(repo: Arc<Mutex<Repo<IO>>>, listener: TcpListener) -> io::Result<()>
where
    IO: FileIO + Send + 'static,
{
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let repo = Arc::clone(&repo);
                thread::spawn(move || {
                    if let Err(e) = handle_conn(s, repo) {
                        eprintln!("connection error: {}", e);
                    }
                });
            }
            Err(e) => eprintln!("accept error: {}", e),
        }
    }
    Ok(())
}

/// Handles one connection (one request, Connection: close).
pub fn handle_conn<IO: FileIO>(stream: TcpStream, repo: Arc<Mutex<Repo<IO>>>) -> io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let request = read_request(&mut reader)?;
    let response = match request {
        Ok(req) => route(&req, &repo),
        Err(msg) => respond(400, &format!("{{\"error\":{}}}", quoted(&msg))),
    };
    let mut s = stream;
    s.write_all(response.as_bytes())?;
    s.flush()
}

struct Request {
    method: String,
    path: String,
    query: Vec<(String, String)>,
    body: Vec<u8>,
}

fn read_request(reader: &mut BufReader<TcpStream>) -> io::Result<Result<Request, String>> {
    let mut line = String::new();
    if reader.read_line(&mut line)? == 0 {
        return Ok(Err("empty request".into()));
    }
    let mut parts = line.trim_end().split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("").to_string();
    if method.is_empty() || target.is_empty() {
        return Ok(Err("malformed request line".into()));
    }

    let mut content_length = 0usize;
    let mut header_bytes = line.len();
    loop {
        let mut h = String::new();
        let n = reader.read_line(&mut h)?;
        header_bytes += n;
        if header_bytes > MAX_HEADER_BYTES {
            return Ok(Err("headers too large".into()));
        }
        let trimmed = h.trim_end();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if content_length > MAX_BODY_BYTES {
        return Ok(Err("body too large".into()));
    }
    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;

    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), parse_query(q)),
        None => (target, vec![]),
    };
    Ok(Ok(Request { method, path, query, body }))
}

fn parse_query(q: &str) -> Vec<(String, String)> {
    q.split('&')
        .filter(|s| !s.is_empty())
        .map(|kv| match kv.split_once('=') {
            Some((k, v)) => (url_decode(k), url_decode(v)),
            None => (url_decode(kv), String::new()),
        })
        .collect()
}

fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(b) => out.push(b),
                    Err(_) => out.push(b'%'),
                }
                i += 3;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn query_param<'a>(req: &'a Request, key: &str) -> Option<&'a str> {
    req.query
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

fn route<IO: FileIO>(req: &Request, repo: &Arc<Mutex<Repo<IO>>>) -> String {
    let result = match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/health") => ok(r#"{"ok":true}"#.to_string()),
        ("POST", "/ingest") => handle_ingest(req, repo),
        ("POST", "/flush") => handle_flush(req, repo),
        ("GET", "/query") => handle_query(req, repo),
        ("GET", "/stats") => handle_stats(req, repo),
        _ => Err((404, "not found".to_string())),
    };
    match result {
        Ok(body) => respond(200, &body),
        Err((code, msg)) => respond(code, &format!("{{\"error\":{}}}", quoted(&msg))),
    }
}

fn quoted(s: &str) -> String {
    format!("\"{}\"", json::escape(s))
}

fn ok(body: String) -> Result<String, (u16, String)> {
    Ok(body)
}

fn handle_ingest<IO: FileIO>(
    req: &Request,
    repo: &Arc<Mutex<Repo<IO>>>,
) -> Result<String, (u16, String)> {
    let body: Json = json::parse(std::str::from_utf8(&req.body).map_err(|_| {
        (400, "body is not valid UTF-8".to_string())
    })?)
    .map_err(|e| (400, format!("invalid JSON: {}", e)))?;

    let series = body
        .get("series")
        .and_then(Json::as_str)
        .ok_or((400, "missing \"series\"".to_string()))?
        .to_string();
    let points_json = body
        .get("points")
        .and_then(Json::as_arr)
        .ok_or((400, "missing \"points\" array".to_string()))?;
    let mut points = Vec::with_capacity(points_json.len());
    for p in points_json {
        let pair = p
            .as_arr()
            .ok_or((400, "each point must be [ts, val]".to_string()))?;
        if pair.len() != 2 {
            return Err((400, "each point must be [ts, val]".to_string()));
        }
        let ts = pair[0]
            .as_i64()
            .ok_or((400, "ts must be an integer".to_string()))?;
        let val = pair[1]
            .as_i64()
            .ok_or((400, "val must be an integer".to_string()))?;
        points.push((ts, val));
    }

    let mut repo = repo.lock().map_err(|_| (500, "repo lock poisoned".to_string()))?;
    let outcome = repo
        .append(&series, &points)
        .map_err(|e| (500, format!("append failed: {}", e)))?;

    let rejected: Vec<String> = outcome
        .rejected
        .iter()
        .map(|r| {
            format!(
                "{{\"ts\":{},\"val\":{},\"reason\":{}}}",
                r.ts,
                r.val,
                quoted(r.reason)
            )
        })
        .collect();
    ok(format!(
        "{{\"series\":{},\"accepted\":{},\"buffered\":{},\"rejected\":[{}]}}",
        quoted(&series),
        outcome.accepted,
        outcome.buffered,
        rejected.join(",")
    ))
}

fn handle_flush<IO: FileIO>(
    req: &Request,
    repo: &Arc<Mutex<Repo<IO>>>,
) -> Result<String, (u16, String)> {
    let series = if !req.body.is_empty() {
        json::parse(std::str::from_utf8(&req.body).map_err(|_| (400, "bad UTF-8".to_string()))?)
            .map_err(|e| (400, format!("invalid JSON: {}", e)))?
            .get("series")
            .and_then(Json::as_str)
            .ok_or((400, "missing \"series\"".to_string()))?
            .to_string()
    } else {
        query_param(req, "series")
            .ok_or((400, "missing series".to_string()))?
            .to_string()
    };
    let mut repo = repo.lock().map_err(|_| (500, "repo lock poisoned".to_string()))?;
    repo.flush(&series)
        .map_err(|e| (500, format!("flush failed: {}", e)))?;
    ok(format!("{{\"series\":{},\"flushed\":true}}", quoted(&series)))
}

fn handle_query<IO: FileIO>(
    req: &Request,
    repo: &Arc<Mutex<Repo<IO>>>,
) -> Result<String, (u16, String)> {
    let series = query_param(req, "series").ok_or((400, "missing series".to_string()))?;
    let from: i64 = query_param(req, "from")
        .ok_or((400, "missing from".to_string()))?
        .parse()
        .map_err(|_| (400, "from must be an integer".to_string()))?;
    let to: i64 = query_param(req, "to")
        .ok_or((400, "missing to".to_string()))?
        .parse()
        .map_err(|_| (400, "to must be an integer".to_string()))?;

    let repo = repo.lock().map_err(|_| (500, "repo lock poisoned".to_string()))?;
    let points = repo
        .query(series, from, to)
        .map_err(|e| (400, format!("query failed: {}", e)))?;
    let body: Vec<String> = points.iter().map(|p| format!("[{},{}]", p.0, p.1)).collect();
    ok(format!(
        "{{\"series\":{},\"from\":{},\"to\":{},\"count\":{},\"points\":[{}]}}",
        quoted(series),
        from,
        to,
        points.len(),
        body.join(",")
    ))
}

fn handle_stats<IO: FileIO>(
    req: &Request,
    repo: &Arc<Mutex<Repo<IO>>>,
) -> Result<String, (u16, String)> {
    let series = query_param(req, "series").ok_or((400, "missing series".to_string()))?;
    let repo = repo.lock().map_err(|_| (500, "repo lock poisoned".to_string()))?;
    let s = repo
        .stats(series)
        .map_err(|e| (500, format!("stats failed: {}", e)))?;
    let ratio = if s.file_bytes > 0 {
        s.raw_bytes as f64 / s.file_bytes as f64
    } else {
        0.0
    };
    ok(format!(
        "{{\"series\":{},\"points\":{},\"pending\":{},\"blocks\":{},\"late_points\":{},\"raw_bytes\":{},\"file_bytes\":{},\"late_bytes\":{},\"compression_ratio\":{:.4}}}",
        quoted(series),
        s.points,
        s.pending,
        s.blocks,
        s.late_points,
        s.raw_bytes,
        s.file_bytes,
        s.late_bytes,
        ratio
    ))
}

fn respond(code: u16, body: &str) -> String {
    let reason = match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "Status",
    };
    format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        code,
        reason,
        body.len(),
        body
    )
}
