//! Local HTTP validation endpoint.
//!
//! Deliberately dependency-free HTTP/1.1 (one request per connection,
//! `Connection: close`). This server exists to validate the engine over a
//! real socket — it is not a production web server.
//!
//! ## Routes
//!
//! ```text
//! GET  /healthz
//!      -> 200 "ok\n"
//!
//! POST /sort?job=<id>&mem=<bytes>&buf=<bytes>&key=<spec>&fanin=<k>
//!      body = raw newline-delimited input
//!      -> 200, body = sorted output, X-Sort-* stat headers
//!
//! GET  /sort?job=<id>&mem=<bytes>&buf=<bytes>&key=<spec>&fanin=<k>
//!      -> resumes / re-runs the same job (no body) and streams the output
//!
//! GET  /sort?job=<id>&result=1
//!      -> streams an already-finished job's output.txt (404 if not done)
//! ```
//!
//! A POST with an existing but unfinished job id acts as resume: the uploaded
//! body must be empty and the parameters must match the recorded configuration.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::thread;

use crate::engine::{Engine, SortConfig, SortStats};
use crate::error::Error;
use crate::fs::{BufReader, Fs, RealFs, RFile, WFile};
use crate::key::KeySpec;

pub struct Server {
    listener: TcpListener,
    fs: Arc<dyn Fs>,
}

impl Server {
    pub fn bind(addr: &str, root: &Path) -> std::io::Result<Self> {
        let listener = TcpListener::bind(addr)?;
        let fs: Arc<dyn Fs> = Arc::new(RealFs::new(root.to_path_buf()));
        fs.create_dir_all("jobs").ok();
        Ok(Server { listener, fs })
    }

    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }

    pub fn run(self) -> std::io::Result<()> {
        eprintln!("[extsort] listening on {}", self.listener.local_addr()?);
        for stream in self.listener.incoming() {
            match stream {
                Ok(stream) => {
                    let fs = self.fs.clone();
                    thread::spawn(move || {
                        if let Err(e) = handle_connection(stream.try_clone().unwrap(), fs) {
                            eprintln!("[extsort] connection error: {e}");
                        }
                    });
                }
                Err(e) => eprintln!("[extsort] accept error: {e}"),
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Request parsing
// ---------------------------------------------------------------------------

struct Request {
    method: String,
    path: String,
    query: HashMap<String, String>,
    content_length: usize,
}

/// Reads one HTTP/1.x request head up to and including the blank line using a
/// fixed-size streaming buffer (no external dependencies).
fn read_head(stream: &TcpStream) -> std::io::Result<Vec<u8>> {
    let mut stream = stream.try_clone()?;
    let mut head = Vec::with_capacity(2048);
    let mut buf = [0u8; 1];
    let max_head = 64 * 1024;

    loop {
        if stream.read(&mut buf)? == 0 {
            break;
        }
        head.push(buf[0]);
        if head.len() > max_head {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "head too large"));
        }
        if head.ends_with(b"\r\n\r\n") || head.ends_with(b"\n\n") {
            return Ok(head);
        }
    }
    Ok(head)
}

/// Streams the body into an engine [`WFile`] (our own trait).
fn stream_body_to_wfile(
    stream: &TcpStream,
    len: usize,
    out: &mut impl WFile,
) -> std::io::Result<()> {
    let mut stream = stream.try_clone()?;
    let mut remaining = len;
    let mut buf = vec![0u8; 4096];
    while remaining > 0 {
        let take = remaining.min(buf.len());
        stream.read_exact(&mut buf[..take])?;
        out.write_all(&buf[..take])?;
        remaining -= take;
    }
    out.flush()
}

fn read_request(stream: &TcpStream) -> Result<Request, String> {
    let head = read_head(stream).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&head);
    let mut lines = text.lines();
    let request_line = lines.next().ok_or("empty request")?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().ok_or("missing method")?.to_owned();
    let target = parts.next().ok_or("missing target")?;
    let version = parts.next().unwrap_or("HTTP/1.1");
    if !version.starts_with("HTTP/1.") {
        return Err(format!("unsupported version {version}"));
    }

    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_owned(), parse_query(q)),
        None => (target.to_owned(), HashMap::new()),
    };

    let mut content_length = 0usize;
    for line in lines {
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            content_length = v.trim().parse().map_err(|_| "bad content-length".to_string())?;
        }
    }

    Ok(Request { method, path, query, content_length })
}

fn parse_query(q: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for pair in q.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            map.insert(percent_decode(k), percent_decode(v));
        } else if !pair.is_empty() {
            map.insert(percent_decode(pair), String::new());
        }
    }
    map
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let h = std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(b) = h {
                out.push(b);
                i += 3;
                continue;
            }
        }
        if bytes[i] == b'+' {
            out.push(b' ');
        } else {
            out.push(bytes[i]);
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ---------------------------------------------------------------------------
// Connection handling
// ---------------------------------------------------------------------------

const DEFAULT_MEM: usize = 64 * 1024;
const DEFAULT_BUF: usize = 4096;
const DEFAULT_FANIN: usize = 8;
const MAX_INPUT: u64 = 2 * 1024 * 1024 * 1024; // 2 GiB guard for this validator

fn handle_connection(mut stream: TcpStream, fs: Arc<dyn Fs>) -> Result<(), String> {
    let req = match read_request(&stream) {
        Ok(r) => r,
        Err(e) => {
            write_simple(&mut stream, 400, &format!("bad request: {e}\n")).ok();
            return Ok(());
        }
    };

    let result: Result<(), Error> = match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/healthz") => write_simple(&mut stream, 200, "ok\n").map_err(Error::from),
        ("POST", "/sort") => handle_sort(&mut stream, &fs, &req, true),
        ("GET", "/sort") if req.query.contains_key("result") => handle_result(&mut stream, &fs, &req),
        ("GET", "/sort") => handle_sort(&mut stream, &fs, &req, false),
        _ => write_simple(&mut stream, 404, "not found\n").map_err(Error::from),
    };

    if let Err(e) = result {
        let status = e.http_status();
        let body = format!("{e}\n");
        eprintln!("[extsort] {} {} -> {status}: {e}", req.method, req.path);
        write_simple(&mut stream, status, &body).ok();
    }
    stream.flush().ok();
    Ok(())
}

fn parse_usize(q: &HashMap<String, String>, key: &str, default: usize) -> Result<usize, Error> {
    match q.get(key) {
        None => Ok(default),
        Some(v) => v
            .parse::<usize>()
            .map_err(|_| Error::bad(format!("parameter '{key}' must be an unsigned integer"))),
    }
}

fn build_config(q: &HashMap<String, String>) -> Result<SortConfig, Error> {
    let mem = parse_usize(q, "mem", DEFAULT_MEM)?;
    let buf = parse_usize(q, "buf", DEFAULT_BUF)?;
    let fanin = match q.get("fanin") {
        Some(v) => Some(
            v.parse::<usize>()
                .map_err(|_| Error::bad("parameter 'fanin' must be an unsigned integer"))?,
        ),
        None => Some(DEFAULT_FANIN),
    };
    let key = KeySpec::parse(q.get("key").map_or("", String::as_str))?;
    Ok(SortConfig { key, mem, buf, fanin })
}

fn handle_sort(
    stream: &mut TcpStream,
    fs: &Arc<dyn Fs>,
    req: &Request,
    has_body: bool,
) -> Result<(), Error> {
    let job = req
        .query
        .get("job")
        .cloned()
        .unwrap_or_else(|| "default".to_owned());
    if job.is_empty()
        || job.len() > 128
        || !job
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err(Error::bad("job must match [A-Za-z0-9_-]{1,128}"));
    }
    let cfg = build_config(&req.query)?;

    let job_dir = format!("jobs/{job}");
    let input_rel = format!("{job_dir}/input.txt");
    let output_rel = format!("{job_dir}/output.txt");
    fs.create_dir_all(&job_dir)?;

    let body_len = req.content_length as u64;
    if has_body {
        if body_len > MAX_INPUT {
            return Err(Error::bad("input too large for validator (2GiB limit)"));
        }
        if fs.exists(&input_rel)? {
            // Resume: body must be empty.
            if body_len != 0 {
                return Err(Error::conflict(format!(
                    "job '{job}' already exists; re-POST without a body or use a new job id"
                )));
            }
        } else {
            // Stream body straight to a temp input file using a bounded buffer.
            let tmp_rel = format!("{input_rel}.upload");
            let raw = fs.create_write(&tmp_rel)?;
            let mut writer = crate::fs::BufWriter::new(raw, cfg.buf.max(1024));
            stream_body_to_wfile(stream, body_len as usize, &mut writer)?;
            writer.sync_all()?;
            drop(writer);
            fs.rename(&tmp_rel, &input_rel)?;
            fs.sync_dir(&job_dir)?;
        }
    } else if !fs.exists(&input_rel)? {
        return Err(Error::not_found(format!(
            "job '{job}' has no input; POST the input first"
        )));
    }

    let engine = Engine::new(fs.clone());
    let stats = engine.run(&job, &cfg)?;
    stream_result(stream, &output_rel, fs.as_ref(), &stats, cfg.buf)
}

fn handle_result(stream: &mut TcpStream, fs: &Arc<dyn Fs>, req: &Request) -> Result<(), Error> {
    let job = req.query.get("job").ok_or_else(|| Error::bad("missing job"))?;
    let output_rel = format!("jobs/{job}/output.txt");
    if !fs.exists(&output_rel)? {
        return Err(Error::not_found(format!("no finished result for job '{job}'")));
    }
    let buf = parse_usize(&req.query, "buf", DEFAULT_BUF)?;
    let stats = SortStats { output_rel: output_rel.clone(), ..Default::default() };
    stream_result(stream, &output_rel, fs.as_ref(), &stats, buf)
}

fn stat_headers(stats: &SortStats) -> String {
    format!(
        "X-Sort-Records: {}\r\n\
         X-Sort-Runs: {}\r\n\
         X-Sort-Merge-Rounds: {}\r\n\
         X-Sort-Merge-Steps: {}\r\n\
         X-Sort-Runs-Reused: {}\r\n\
         X-Sort-Merges-Reused: {}\r\n\
         X-Sort-Fanin: {}\r\n\
         X-Sort-Peak-Bytes: {}\r\n\
         X-Sort-Resumed: {}\r\n",
        stats.input_records,
        stats.runs,
        stats.merge_rounds,
        stats.merge_steps,
        stats.runs_reused,
        stats.merges_reused,
        stats.fanin,
        stats.peak_bytes,
        if stats.resumed { "1" } else { "0" },
    )
}

fn stream_result(
    stream: &mut TcpStream,
    output_rel: &str,
    fs: &dyn Fs,
    stats: &SortStats,
    buf_size: usize,
) -> Result<(), Error> {
    let meta = std::fs::metadata(fs.root().join(output_rel.trim_start_matches('/')))?;
    let len = meta.len();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {len}\r\n\
         {}Connection: close\r\n\r\n",
        stat_headers(stats)
    )?;
    stream.flush()?;

    let file = fs.open_read(output_rel)?;
    let mut reader = BufReader::new(file, buf_size.max(64));
    let mut buf = vec![0u8; buf_size.max(64)];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        stream.write_all(&buf[..n])?;
    }
    stream.flush()?;
    Ok(())
}

fn write_simple(stream: &mut TcpStream, status: u16, body: &str) -> std::io::Result<()> {
    let reason = reason_phrase(status);
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        500 => "Internal Server Error",
        _ => "Error",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_and_percent_decode() {
        let q = parse_query("job=j1&mem=1024&key=1%3Adesc");
        assert_eq!(q.get("job").unwrap(), "j1");
        assert_eq!(q.get("mem").unwrap(), "1024");
        assert_eq!(q.get("key").unwrap(), "1:desc");
        assert_eq!(percent_decode("a+b%3Bc"), "a b;c");
    }
}
