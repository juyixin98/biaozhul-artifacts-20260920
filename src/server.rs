//! Minimal local HTTP validation server (std-only, no async runtime).
//!
//! ## Endpoints
//!
//! | Method & path                 | Purpose |
//! |-------------------------------|---------|
//! | `GET  /healthz`               | liveness, `ok` |
//! | `POST /jobs?…`                | create a job, body = raw input, run it |
//! | `POST /jobs/{id}/resume`      | resume a job stopped by an injected fault |
//! | `GET  /jobs`                  | list job ids |
//! | `GET  /jobs/{id}`             | status JSON (`status.json`) |
//! | `GET  /jobs/{id}/output`      | sorted output bytes |
//! | `DELETE /jobs/{id}`           | remove a job directory |
//!
//! Query parameters for `POST /jobs`:
//! `spec` (composite key text), `delim` (one byte, default `,`),
//! `budget` (bytes), `lanes` (merge fan-in, default 4),
//! `keep_temp` (`1` to retain intermediates).
//!
//! Fault injection: pass `X-Fault-Inject: <grammar>` on a `POST /jobs` to wrap
//! the repository's VFS in a [`crate::io::FaultVfs`]. This is the local
//! validation harness hook for crash/failure simulation.
//!
//! Bodies are read using `Content-Length` (chunked transfer encoding is
//! rejected with 411/400 — the validation client always sets a length).

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::thread;

use crate::error::{Error, Result};
use crate::io::{FaultPlan, FaultVfs, RealVfs};
use crate::key::KeySpec;
use crate::repository::{JobConfig, Repository};
use crate::sort::run_job;

/// Server configuration.
pub struct ServerConfig {
    pub root: String,
    pub addr: String,
}

/// Start serving and block on the listener.
pub fn serve(cfg: ServerConfig) -> Result<()> {
    let listener = TcpListener::bind(&cfg.addr).map_err(|e| Error::io(e, &cfg.addr))?;
    let local = listener.local_addr().map_err(|e| Error::io(e, &cfg.addr))?;
    eprintln!("extsort listening on http://{local} (root {})", cfg.root);

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let root = cfg.root.clone();
                thread::spawn(move || {
                    if let Err(e) = handle_connection(stream, &root) {
                        eprintln!("connection error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

fn handle_connection(mut stream: TcpStream, root: &str) -> Result<()> {
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(60)))
        .ok();
    stream
        .set_write_timeout(Some(std::time::Duration::from_secs(60)))
        .ok();

    let req = match parse_request(&mut stream) {
        Ok(r) => r,
        Err(e) => {
            send_simple(&mut stream, 400, "Bad Request", &format!("{e}"))?;
            return Ok(());
        }
    };

    let path_only = req.path.split('?').next().unwrap_or("/").to_string();
    let query = req
        .path
        .split_once('?')
        .map(|(_, q)| parse_query(q))
        .unwrap_or_default();

    match (req.method.as_str(), path_only.as_str()) {
        ("GET", "/healthz") => send_simple(&mut stream, 200, "OK", "ok\n"),
        ("GET", "/jobs") => list_jobs(&mut stream, root),
        ("POST", "/jobs") => create_and_run(&mut stream, root, &query, &req),
        ("POST", p) if p.starts_with("/jobs/") && p.ends_with("/resume") => {
            let id = &p["/jobs/".len()..p.len() - "/resume".len()];
            resume_job(&mut stream, root, id)
        }
        ("GET", p) if p.starts_with("/jobs/") && p.ends_with("/output") => {
            let id = &p["/jobs/".len()..p.len() - "/output".len()];
            get_output(&mut stream, root, id)
        }
        ("GET", p) if p.starts_with("/jobs/") => {
            let id = &p["/jobs/".len()..];
            get_status(&mut stream, root, id)
        }
        ("DELETE", p) if p.starts_with("/jobs/") => {
            let id = &p["/jobs/".len()..];
            delete_job(&mut stream, root, id)
        }
        _ => send_simple(&mut stream, 404, "Not Found", "no such route\n"),
    }
}

struct Request {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

fn parse_request(stream: &mut TcpStream) -> Result<Request> {
    // Read headers up to CRLF CRLF.
    let mut buf = Vec::with_capacity(4096);
    let mut byte = [0u8; 1];
    let mut header_end = None;
    while header_end.is_none() {
        let n = stream.read(&mut byte)?;
        if n == 0 {
            break;
        }
        buf.push(byte[0]);
        if buf.len() >= 4 && &buf[buf.len() - 4..] == b"\r\n\r\n" {
            header_end = Some(buf.len() - 4);
        }
        if buf.len() > 64 * 1024 {
            return Err(Error::BadRequest("headers too large".into()));
        }
    }
    let end = header_end.ok_or_else(|| Error::BadRequest("no header terminator".into()))?;
    let head = String::from_utf8_lossy(&buf[..end]).into_owned();
    let mut lines = head.split("\r\n");
    let request_line = lines
        .next()
        .ok_or_else(|| Error::BadRequest("empty request".into()))?;
    let mut parts = request_line.split_whitespace();
    let method = parts
        .next()
        .ok_or_else(|| Error::BadRequest("no method".into()))?
        .to_string();
    let path = parts
        .next()
        .ok_or_else(|| Error::BadRequest("no path".into()))?
        .to_string();

    let mut headers = HashMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_lowercase(), v.trim().to_string());
        }
    }

    // Read body per Content-Length; bytes beyond headers already consumed are
    // none here (we read header one byte at a time), so body starts fresh.
    let mut body = Vec::new();
    if let Some(len_txt) = headers.get("content-length") {
        let len: usize = len_txt
            .parse()
            .map_err(|_| Error::BadRequest("bad content-length".into()))?;
        body = vec![0u8; len];
        read_full(stream, &mut body)?;
    } else if headers.contains_key("transfer-encoding") {
        return Err(Error::BadRequest(
            "chunked request bodies are not supported; set Content-Length".into(),
        ));
    }

    Ok(Request {
        method,
        path,
        headers,
        body,
    })
}

fn read_full(stream: &mut TcpStream, buf: &mut [u8]) -> Result<()> {
    let mut got = 0;
    while got < buf.len() {
        let n = stream.read(&mut buf[got..])?;
        if n == 0 {
            return Err(Error::BadRequest("request body truncated".into()));
        }
        got += n;
    }
    Ok(())
}

fn parse_query(q: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    for pair in q.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            m.insert(urldecode(k), urldecode(v));
        } else if !pair.is_empty() {
            m.insert(urldecode(pair), String::new());
        }
    }
    m
}

fn urldecode(s: &str) -> String {
    let b = s.replace('+', " ");
    let bytes = b.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn send_simple(stream: &mut TcpStream, status: u16, reason: &str, body: &str) -> Result<()> {
    send_bytes(
        stream,
        status,
        reason,
        "text/plain; charset=utf-8",
        body.as_bytes(),
    )
}

fn send_bytes(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    content_type: &str,
    body: &[u8],
) -> Result<()> {
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;
    Ok(())
}

fn send_json(stream: &mut TcpStream, status: u16, reason: &str, json: String) -> Result<()> {
    send_bytes(stream, status, reason, "application/json", json.as_bytes())
}

fn list_jobs(stream: &mut TcpStream, root: &str) -> Result<()> {
    let repo = Repository::new(root);
    let ids = repo.list_jobs()?;
    let json = format!(
        "{{\"jobs\":[{}]}}\n",
        ids.iter()
            .map(|i| format!("\"{}\"", crate::repository::json_escape(i)))
            .collect::<Vec<_>>()
            .join(",")
    );
    send_json(stream, 200, "OK", json)
}

fn get_status(stream: &mut TcpStream, root: &str, id: &str) -> Result<()> {
    if id.is_empty() || id.contains('/') || id.contains("..") {
        return send_simple(stream, 400, "Bad Request", "bad job id\n");
    }
    let repo = Repository::new(root);
    match repo.read_status(id) {
        Ok(s) => send_json(stream, 200, "OK", s),
        Err(e) => send_error(stream, &e),
    }
}

fn get_output(stream: &mut TcpStream, root: &str, id: &str) -> Result<()> {
    if id.is_empty() || id.contains('/') || id.contains("..") {
        return send_simple(stream, 400, "Bad Request", "bad job id\n");
    }
    let repo = Repository::new(root);
    let vfs = repo.vfs();
    let path = format!("jobs/{id}/output.txt");
    match crate::io::read_all(vfs.as_ref(), &path) {
        Ok(bytes) => send_bytes(stream, 200, "OK", "application/octet-stream", &bytes),
        Err(e) => send_error(stream, &e),
    }
}

fn delete_job(stream: &mut TcpStream, root: &str, id: &str) -> Result<()> {
    if id.is_empty() || id.contains('/') || id.contains("..") {
        return send_simple(stream, 400, "Bad Request", "bad job id\n");
    }
    let repo = Repository::new(root);
    repo.vfs().remove_dir_all(&format!("jobs/{id}"))?;
    send_json(stream, 200, "OK", format!("{{\"deleted\":\"{id}\"}}\n"))
}

fn create_and_run(
    stream: &mut TcpStream,
    root: &str,
    q: &HashMap<String, String>,
    req: &Request,
) -> Result<()> {
    let cfg = match parse_config(q) {
        Ok(c) => c,
        Err(e) => return send_error(stream, &e),
    };

    // Validate the key spec up front for a clean 400.
    if let Err(e) = KeySpec::parse(&cfg.key_spec, cfg.delim) {
        return send_error(stream, &e);
    }

    let id = new_job_id();

    // Optional fault injection for this single request.
    let fault_text = req
        .headers
        .get("x-fault-inject")
        .cloned()
        .unwrap_or_default();
    let result = if fault_text.is_empty() {
        run_with_repo(root, &id, &cfg, &req.body, None)
    } else {
        match FaultPlan::parse(&fault_text) {
            Ok(plan) => run_with_repo(root, &id, &cfg, &req.body, Some(plan)),
            Err(msg) => {
                return send_simple(
                    stream,
                    400,
                    "Bad Request",
                    &format!("invalid X-Fault-Inject: {msg}\n"),
                )
            }
        }
    };

    match result {
        Ok(summary) => send_json(stream, 200, "OK", summary),
        Err(e) => send_error_with_body(stream, root, &id, &e),
    }
}

fn resume_job(stream: &mut TcpStream, root: &str, id: &str) -> Result<()> {
    if id.is_empty() || id.contains('/') || id.contains("..") {
        return send_simple(stream, 400, "Bad Request", "bad job id\n");
    }
    let repo = Repository::new(root);
    let mut job = repo.open_job(id)?;
    let stats = run_job(&repo, &mut job)?;
    let summary = format!(
        "{{\"job_id\":\"{id}\",\"state\":\"done\",\"records\":{},\"level0_runs\":{},\"merge_passes\":{},\"merge_batches\":{},\"resumed\":true,\"output_bytes\":{},\"output\":\"/jobs/{id}/output\"}}\n",
        stats.records, stats.level0_runs, stats.merge_passes, stats.merge_batches, stats.output_bytes
    );
    send_json(stream, 200, "OK", summary)
}

fn parse_config(q: &HashMap<String, String>) -> Result<JobConfig> {
    let mut cfg = JobConfig::default();
    if let Some(v) = q.get("spec") {
        cfg.key_spec = v.clone();
    }
    if let Some(v) = q.get("delim") {
        let b = v.as_bytes();
        if b.len() != 1 {
            return Err(Error::Config("delim must be exactly one byte".into()));
        }
        cfg.delim = b[0];
    }
    if let Some(v) = q.get("budget") {
        cfg.budget = v
            .parse()
            .map_err(|_| Error::Config("budget must be a positive integer".into()))?;
    }
    if let Some(v) = q.get("lanes") {
        cfg.max_lanes = v
            .parse()
            .map_err(|_| Error::Config("lanes must be an integer >= 2".into()))?;
    }
    if let Some(v) = q.get("keep_temp") {
        cfg.keep_temp = v == "1" || v.eq_ignore_ascii_case("true");
    }
    Ok(cfg)
}

fn run_with_repo(
    root: &str,
    id: &str,
    cfg: &JobConfig,
    body: &[u8],
    fault: Option<FaultPlan>,
) -> Result<String> {
    let repo = match fault {
        Some(plan) => {
            let real = Arc::new(RealVfs::new(Path::new(root)));
            let fault_vfs = Arc::new(FaultVfs::new(real, plan));
            Repository::with_vfs(root, fault_vfs)
        }
        None => Repository::new(root),
    };
    let vfs = repo.vfs();

    let mut job = repo.create_job(id, cfg.clone())?;
    // Persist the raw input.
    let input_tmp = format!("jobs/{id}/input.dat.tmp");
    let input_dst = format!("jobs/{id}/input.dat");
    crate::io::atomic_write_bytes(vfs.as_ref(), &input_tmp, &input_dst, body)?;

    let stats = run_job(&repo, &mut job);
    match stats {
        Ok(s) => Ok(format!(
            "{{\"job_id\":\"{id}\",\"state\":\"done\",\"records\":{},\"level0_runs\":{},\"merge_passes\":{},\"merge_batches\":{},\"resumed\":{},\"output_bytes\":{},\"output\":\"/jobs/{id}/output\"}}\n",
            s.records, s.level0_runs, s.merge_passes, s.merge_batches, s.resumed, s.output_bytes
        )),
        Err(e) => {
            repo.record_fault(&mut job, &e.to_string());
            Err(e)
        }
    }
}

fn send_error(stream: &mut TcpStream, e: &Error) -> Result<()> {
    let status = e.http_status();
    let reason = reason_phrase(status);
    let body = format!(
        "{{\"error\":\"{}\",\"code\":\"{}\"}}\n",
        crate::repository::json_escape(&e.to_string()),
        e.code()
    );
    send_json(stream, status, reason, body)
}

/// After a failed run the status.json holds the durable failure; surface it.
fn send_error_with_body(stream: &mut TcpStream, root: &str, id: &str, e: &Error) -> Result<()> {
    let status = e.http_status();
    let reason = reason_phrase(status);
    let detail = Repository::new(root)
        .read_status(id)
        .unwrap_or_else(|_| "{}".into());
    let body = format!(
        "{{\"job_id\":\"{id}\",\"error\":\"{}\",\"code\":\"{}\",\"status\":{}}}\n",
        crate::repository::json_escape(&e.to_string()),
        e.code(),
        detail.trim()
    );
    send_json(stream, status, reason, body)
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        _ => "Internal Server Error",
    }
}

fn new_job_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    // Cheap uniqueness without external crates: nanos + pid + a mutating tail.
    let pid = std::process::id();
    let tail = (nanos & 0xFFFF) as u32;
    format!("j-{:x}-{:x}", nanos, pid ^ tail)
}
