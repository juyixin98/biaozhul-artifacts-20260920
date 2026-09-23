//! Minimal dependency-free HTTP validation frontend.
//!
//! One request per connection (`Connection: close`), one thread per
//! connection. This is intentionally small — its job is to make the repository
//! observable and testable over the network, not to be a production web stack.
//!
//! ## Routes
//!
//! | Method | Path                       | Purpose                                            |
//! |--------|----------------------------|----------------------------------------------------|
//! | POST   | `/blocks`                  | upload raw bytes; hash is computed by the server   |
//! | PUT    | `/blocks/{hash}`           | content-addressed upload; integrity-checked        |
//! | GET    | `/blocks/{hash}`           | download (`?verify=1` re-hashes before serving)    |
//! | HEAD   | `/blocks/{hash}`           | existence / size                                   |
//! | PUT    | `/roots/{name}`            | atomically publish/republish a named root          |
//! | GET    | `/roots`, `/roots/{name}`  | list / read roots                                  |
//! | DELETE | `/roots/{name}`            | delete a root (blocks become GC-orphans)           |
//! | POST   | `/gc?dry_run=0\|1`         | mark-sweep collection                              |
//! | POST   | `/verify`                  | full integrity scan                                |
//! | GET    | `/healthz`                 | liveness                                           |
//!
//! Outgoing refs for an upload are declared with the `X-Refs` header:
//! comma-separated 64-hex hashes. A JSON body of
//! `{"data": "<hex>", "refs": ["..."]}` is also accepted on `POST /blocks`.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde::Serialize;

use crate::store::{Repository, StoreError};

const MAX_BODY: usize = 64 * 1024 * 1024;
const HEADER_TIMEOUT: Duration = Duration::from_secs(10);

/// Run the HTTP server until the process is killed.
pub fn serve(repo: Repository, addr: &str) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    eprintln!("cas-store listening on http://{addr}");
    eprintln!("repository at {}", repo.path().display());
    serve_listener(repo, listener)
}

/// Serve on an already-bound listener (lets tests pick an ephemeral port).
pub fn serve_listener(repo: Repository, listener: TcpListener) -> std::io::Result<()> {
    let local = listener
        .local_addr()
        .map(|a| a.to_string())
        .unwrap_or_else(|_| "?".to_string());
    eprintln!("cas-store listening on http://{local}");
    let repo = Arc::new(repo);
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let repo = Arc::clone(&repo);
                thread::spawn(move || {
                    let _ = handle_connection(stream, repo);
                });
            }
            Err(e) => eprintln!("accept failed: {e}"),
        }
    }
    Ok(())
}

#[derive(Debug)]
struct Request {
    method: String,
    path: String,
    query: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(&name))
            .map(|(_, v)| v.as_str())
    }
}

/// Handle a single connection and close it when done. Public so integration
/// tests can drive the real protocol without binding a global server.
pub fn serve_connection(stream: TcpStream, repo: Arc<Repository>) -> std::io::Result<()> {
    handle_connection(stream, repo)
}

fn handle_connection(stream: TcpStream, repo: Arc<Repository>) -> std::io::Result<()> {
    stream.set_read_timeout(Some(HEADER_TIMEOUT))?;
    stream.set_nodelay(true)?;

    let (mut writer, req) = match read_request(stream) {
        Ok(r) => r,
        Err(RequestError::Closed) => return Ok(()),
        Err(RequestError::TooLarge) => {
            // We no longer own the stream here; oversize bodies are reported
            // after the (small) headers are parsed, so this branch only fires
            // for oversize *headers*, for which dropping the connection is an
            // acceptable response.
            return Ok(());
        }
        Err(_) => return Ok(()),
    };

    let response = match req {
        Some(req) => dispatch(&repo, &req),
        None => Response::text(400, "malformed request"),
    };
    write_response(&mut writer, &response)
}

/// Read one full HTTP/1.1 request (headers + Content-Length body) and return
/// the stream to reply on plus the parsed request.
fn read_request(mut stream: TcpStream) -> Result<(TcpStream, Option<Request>), RequestError> {
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    let head_len = loop {
        if buf.len() > 1024 * 1024 {
            return Err(RequestError::TooLarge);
        }
        let n = stream
            .read(&mut byte)
            .map_err(|_| RequestError::Io)?;
        if n == 0 {
            if buf.is_empty() {
                return Err(RequestError::Closed);
            }
            return Err(RequestError::Io);
        }
        buf.push(byte[0]);
        if buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n" {
            break buf.len();
        }
    };

    let mut req = parse_request_head(&buf[..head_len - 4]).map_err(|_| RequestError::Malformed)?;

    let mut body = buf[head_len..].to_vec();
    if let Some(len) = req
        .header("content-length")
        .and_then(|v| v.trim().parse::<usize>().ok())
    {
        if len > MAX_BODY {
            let _ = write_response(&stream, &Response::text(413, "request body too large"));
            return Err(RequestError::Closed);
        }
        let mut chunk = [0u8; 16 * 1024];
        while body.len() < len {
            let want = (len - body.len()).min(chunk.len());
            let n = stream.read(&mut chunk[..want]).map_err(|_| RequestError::Io)?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..n]);
        }
        body.truncate(len);
    }
    req.body = body;
    Ok((stream, Some(req)))
}

#[derive(Debug)]
enum RequestError {
    Malformed,
    TooLarge,
    Closed,
    Io,
}

fn parse_request_head(raw: &[u8]) -> Result<Request, ()> {
    let text = std::str::from_utf8(raw).map_err(|_| ())?;
    let mut lines = text.split("\r\n");
    let status = lines.next().ok_or(())?;
    let mut parts = status.split_whitespace();
    let method = parts.next().ok_or(())?.to_string();
    let target = parts.next().ok_or(())?;
    let _version = parts.next().ok_or(())?;
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target.to_string(), String::new()),
    };
    let mut headers = Vec::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let (k, v) = line.split_once(':').ok_or(())?;
        headers.push((k.trim().to_string(), v.trim().to_string()));
    }
    Ok(Request {
        method,
        path,
        query,
        headers,
        body: Vec::new(),
    })
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

fn dispatch(repo: &Repository, req: &Request) -> Response {
    let result = route(repo, req);
    match result {
        Ok(resp) => resp,
        Err(err) => {
            let status = match &err {
                StoreError::InvalidRequest(_) | StoreError::HashMismatch { .. } => 400,
                StoreError::NotFound(_) => 404,
                StoreError::MissingReference(_) => 409,
                StoreError::Corrupt { .. } => 422,
                StoreError::Locked => 409,
                StoreError::NotARepository(_) => 500,
                StoreError::Io(_) | StoreError::Internal(_) => 500,
            };
            let body = serde_json::json!({
                "error": err.code(),
                "message": err.to_string(),
            });
            Response::json(status, &body)
        }
    }
}

fn route(repo: &Repository, req: &Request) -> Result<Response, StoreError> {
    let path = req.path.as_str();
    match (req.method.as_str(), path) {
        ("GET", "/") | ("GET", "/help") => Ok(index()),
        ("GET", "/healthz") => Ok(Response::json(
            200,
            &serde_json::json!({"status": "ok"}),
        )),

        ("POST", "/blocks") => post_block(repo, req),
        ("PUT", p) if p.starts_with("/blocks/") => put_block(repo, req, &p["/blocks/".len()..]),
        ("GET", p) if p.starts_with("/blocks/") => get_block(repo, &p["/blocks/".len()..], &req.query),
        ("HEAD", p) if p.starts_with("/blocks/") => head_block(repo, &p["/blocks/".len()..]),

        ("GET", "/roots") => Ok(Response::json(200, &repo.list_roots()?)),
        ("PUT", p) if p.starts_with("/roots/") => put_root(repo, req, &p["/roots/".len()..]),
        ("GET", p) if p.starts_with("/roots/") => {
            let name = url_decode(&p["/roots/".len()..]);
            Ok(Response::json(200, &repo.get_root(&name)?))
        }
        ("DELETE", p) if p.starts_with("/roots/") => {
            let name = url_decode(&p["/roots/".len()..]);
            repo.delete_root(&name)?;
            Ok(Response::json(200, &serde_json::json!({"deleted": name})))
        }

        ("POST", "/gc") => {
            let dry_run = query_value(&req.query, "dry_run").map(|v| v != "0")
                .unwrap_or(true);
            Ok(Response::json(200, &repo.gc(dry_run)?))
        }
        ("POST", "/verify") => Ok(Response::json(200, &repo.verify()?)),

        _ => Ok(Response::text(404, "unknown route; GET / for help")),
    }
}

fn parse_refs(header: Option<&str>) -> Result<Vec<String>, StoreError> {
    let Some(raw) = header else {
        return Ok(Vec::new());
    };
    let refs: Vec<String> = raw
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    Ok(refs)
}

fn post_block(repo: &Repository, req: &Request) -> Result<Response, StoreError> {
    let ctype = req.header("content-type").unwrap_or("");
    if ctype.contains("application/json") {
        #[derive(serde::Deserialize)]
        struct JsonUpload {
            /// Hex-encoded content.
            data: String,
            #[serde(default)]
            refs: Vec<String>,
        }
        let parsed: JsonUpload = serde_json::from_slice(&req.body)
            .map_err(|e| StoreError::InvalidRequest(format!("invalid JSON body: {e}")))?;
        let data = hex_decode(&parsed.data)
            .map_err(|e| StoreError::InvalidRequest(format!("data must be hex: {e}")))?;
        let info = repo.add_block(&data, &parsed.refs)?;
        return Ok(Response::json(200, &info));
    }
    // Raw bytes body.
    let refs = parse_refs(req.header("x-refs"))?;
    let info = repo.add_block(&req.body, &refs)?;
    Ok(Response::json(200, &info))
}

fn put_block(repo: &Repository, req: &Request, hash: &str) -> Result<Response, StoreError> {
    let hash = url_decode(hash);
    let ctype = req.header("content-type").unwrap_or("");
    let (data, refs) = if ctype.contains("application/json") {
        #[derive(serde::Deserialize)]
        struct JsonPut {
            data: String,
            #[serde(default)]
            refs: Vec<String>,
        }
        let parsed: JsonPut = serde_json::from_slice(&req.body)
            .map_err(|e| StoreError::InvalidRequest(format!("invalid JSON body: {e}")))?;
        let data = hex_decode(&parsed.data)
            .map_err(|e| StoreError::InvalidRequest(format!("data must be hex: {e}")))?;
        (data, parsed.refs)
    } else {
        (req.body.clone(), parse_refs(req.header("x-refs"))?)
    };
    let info = repo.add_block_named(&hash, &data, &refs)?;
    Ok(Response::json(200, &info))
}

fn get_block(repo: &Repository, hash: &str, query: &str) -> Result<Response, StoreError> {
    let hash = url_decode(hash);
    let verify = query_value(query, "verify").map(|v| v == "1").unwrap_or(false);
    let data = if verify {
        repo.get_block_verified(&hash)?
    } else {
        repo.get_block(&hash)?
    };
    Ok(Response::bytes(200, "application/octet-stream", data))
}

fn head_block(repo: &Repository, hash: &str) -> Result<Response, StoreError> {
    let hash = url_decode(hash);
    let data = repo.get_block(&hash)?;
    let mut resp = Response::bytes(200, "application/octet-stream", Vec::new());
    resp.headers
        .push(("Content-Length".to_string(), data.len().to_string()));
    resp.headers
        .push(("X-Content-Sha256".to_string(), hash));
    Ok(resp)
}

fn put_root(repo: &Repository, req: &Request, name: &str) -> Result<Response, StoreError> {
    let name = url_decode(name);
    let hash = if let Some(h) = req.header("x-block-hash") {
        h.trim().to_string()
    } else if !req.body.is_empty() {
        #[derive(serde::Deserialize)]
        struct RootBody {
            hash: String,
        }
        let b: RootBody = serde_json::from_slice(&req.body)
            .map_err(|e| StoreError::InvalidRequest(format!("root body must be {{\"hash\":...}} or use X-Block-Hash: {e}")))?;
        b.hash
    } else {
        return Err(StoreError::InvalidRequest(
            "root publish needs X-Block-Hash header or JSON body".into(),
        ));
    };
    let manifest = repo.put_root(&name, &hash)?;
    Ok(Response::json(200, &manifest))
}

// ---------------------------------------------------------------------------
// Response
// ---------------------------------------------------------------------------

struct Response {
    status: u16,
    content_type: String,
    body: Vec<u8>,
    headers: Vec<(String, String)>,
}

impl Response {
    fn text(status: u16, body: impl Into<String>) -> Self {
        Response {
            status,
            content_type: "text/plain; charset=utf-8".to_string(),
            body: body.into().into_bytes(),
            headers: Vec::new(),
        }
    }

    fn json<T: Serialize>(status: u16, value: &T) -> Self {
        let body = serde_json::to_vec_pretty(value).unwrap_or_default();
        Response {
            status,
            content_type: "application/json".to_string(),
            body,
            headers: Vec::new(),
        }
    }

    fn bytes(status: u16, content_type: &str, body: Vec<u8>) -> Self {
        Response {
            status,
            content_type: content_type.to_string(),
            body,
            headers: Vec::new(),
        }
    }
}

fn write_response(mut stream: &TcpStream, resp: &Response) -> std::io::Result<()> {
    let reason = reason_phrase(resp.status);
    // HEAD responses carry the size of the resource, not of the (empty) body.
    let content_length = resp
        .headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .map(|(_, v)| v.clone())
        .unwrap_or_else(|| resp.body.len().to_string());
    let mut head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n",
        resp.status, reason, resp.content_type, content_length
    );
    for (k, v) in &resp.headers {
        if k.eq_ignore_ascii_case("content-length") {
            continue; // already emitted above
        }
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    stream.write_all(&resp.body)?;
    stream.flush()
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Payload Too Large",
        422 => "Unprocessable Entity",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn query_value<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v)
}

fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let h = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                if let Ok(v) = u8::from_str_radix(h, 16) {
                    out.push(v);
                    i += 3;
                    continue;
                }
                out.push(bytes[i]);
                i += 1;
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

fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 {
        return Err("odd length".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        let pair = std::str::from_utf8(&b[i..i + 2]).map_err(|_| "non-utf8".to_string())?;
        out.push(u8::from_str_radix(pair, 16).map_err(|e| e.to_string())?);
        i += 2;
    }
    Ok(out)
}

fn index() -> Response {
    Response::text(
        200,
        "cas-store HTTP API\n\n\
         POST   /blocks              upload bytes (X-Refs header or JSON {\"data\":hex,\"refs\":[]})\n\
         PUT    /blocks/<sha256>     content-addressed, integrity-checked upload\n\
         GET    /blocks/<sha256>     download (?verify=1 to re-hash)\n\
         HEAD   /blocks/<sha256>     exists + size\n\
         PUT    /roots/<name>        publish root (X-Block-Hash header or JSON {\"hash\":...})\n\
         GET    /roots               list roots\n\
         GET    /roots/<name>        read root manifest\n\
         DELETE /roots/<name>        delete root\n\
         POST   /gc?dry_run=0|1      mark-sweep GC (default dry_run=1)\n\
         POST   /verify              integrity scan\n\
         GET    /healthz             liveness\n",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_decoding() {
        assert_eq!(url_decode("a%2Fb"), "a/b");
        assert_eq!(url_decode("a+b"), "a b");
        assert_eq!(url_decode("main"), "main");
    }

    #[test]
    fn query_parsing() {
        assert_eq!(query_value("dry_run=0", "dry_run"), Some("0"));
        assert_eq!(query_value("verify=1", "verify"), Some("1"));
        assert_eq!(query_value("a=b&c=d", "c"), Some("d"));
        assert_eq!(query_value("a=b", "c"), None);
    }

    #[test]
    fn hex_roundtrip() {
        assert_eq!(hex_decode("deadbeef").unwrap(), vec![0xde, 0xad, 0xbe, 0xef]);
        assert!(hex_decode("abc").is_err());
        assert!(hex_decode("zz").is_err());
    }
}
