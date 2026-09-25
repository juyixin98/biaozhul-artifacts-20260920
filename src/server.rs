//! Local TCP reference service built on the [`Framer`].
//!
//! The service is deliberately *not* a proxy: it never forwards
//! anything. Every fully framed request gets a `200` with a JSON
//! description of what was framed; every framing failure gets a single
//! `4xx` (carrying the stable error id in `X-Frame-Error`) and the
//! connection is then closed, so a desynchronised sender cannot
//! smuggle a second request behind the rejected one.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

use crate::error::ErrorKind;
use crate::framer::Framer;
use crate::parser::{Framing, Request};
use crate::Limits;

/// Runtime knobs for the reference service.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub addr: String,
    pub limits: Limits,
    /// Size of each `read()`; small values (e.g. 1–8) force the
    /// incremental paths and are used by the smoke tests.
    pub read_size: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            addr: "127.0.0.1:8080".to_string(),
            limits: Limits::default(),
            read_size: 4096,
        }
    }
}

/// Bind and accept forever (one thread per connection).
pub fn run(config: ServerConfig) -> std::io::Result<()> {
    let listener = TcpListener::bind(&config.addr)?;
    eprintln!(
        "http-framing reference service listening on http://{}",
        config.addr
    );
    serve_listener(listener, config)
}

/// Accept loop on an already-bound listener. Split out so tests can
/// bind port 0 and learn the ephemeral address.
pub fn serve_listener(listener: TcpListener, config: ServerConfig) -> std::io::Result<()> {
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let cfg = config.clone();
                thread::spawn(move || {
                    let _ = set_timeouts(&stream);
                    if let Err(e) = handle(&stream, &stream, &cfg) {
                        eprintln!("connection error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

/// Frame one connection. Exposed (and IO-agnostic) so the same logic
/// drives the TCP server and the `--stdio` mode.
pub fn handle<R: Read, W: Write>(mut input: R, mut output: W, config: &ServerConfig) -> std::io::Result<()> {
    let mut framer = Framer::with_limits(config.limits.clone());
    let mut chunk = vec![0u8; config.read_size.max(1)];

    loop {
        let n = input.read(&mut chunk)?;
        if n == 0 {
            // Clean EOF. A partial request still buffered is a
            // protocol error; answer once and stop.
            if framer.pending_bytes() > 0 {
                if let Err(e) = framer.end_input() {
                    write_error(&mut output, e.kind, 0)?;
                }
            }
            return Ok(());
        }
        match framer.feed(&chunk[..n]) {
            Ok(requests) => {
                for req in &requests {
                    write_request_json(&mut output, req)?;
                }
            }
            Err(e) => {
                write_error(&mut output, e.kind, e.offset)?;
                // Drain whatever the peer already sent before closing:
                // dropping a socket with unread bytes queued makes the
                // kernel send RST on Linux, which can hide the 4xx from
                // a client still finishing its write. EOF (or any IO
                // error) ends the connection.
                let mut sink = [0u8; 4096];
                loop {
                    match input.read(&mut sink) {
                        Ok(0) => break,
                        Ok(_) => continue,
                        Err(_) => break,
                    }
                }
                return Ok(());
            }
        }
    }
}

/// Convenience used by tests: connect with a read timeout so a stalled
/// peer cannot hang a worker forever.
pub fn set_timeouts(stream: &TcpStream) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    Ok(())
}

fn write_request_json<W: Write>(w: &mut W, req: &Request) -> std::io::Result<()> {
    let body = describe_request(req);
    let framing = match req.framing {
        Framing::NoBody => "none",
        Framing::ContentLength(_) => "content-length",
        Framing::Chunked => "chunked",
    };
    write!(
        w,
        "HTTP/1.1 200 OK\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         X-Framing: {}\r\n\
         Connection: keep-alive\r\n\
         \r\n",
        body.len(),
        framing
    )?;
    w.write_all(&body)?;
    w.flush()
}

fn write_error<W: Write>(w: &mut W, kind: ErrorKind, offset: usize) -> std::io::Result<()> {
    let status = kind.http_status();
    let reason = if status == 413 {
        "Payload Too Large"
    } else if status == 431 {
        "Request Header Fields Too Large"
    } else {
        "Bad Request"
    };
    let body = format!(
        "{{\"error\":\"{}\",\"offset\":{}}}\n",
        kind.name(),
        offset
    );
    write!(
        w,
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         X-Frame-Error: {}\r\n\
         Connection: close\r\n\
         \r\n",
        body.len(),
        kind.name()
    )?;
    w.write_all(body.as_bytes())?;
    w.flush()
}

/// Build the JSON description by hand (the crate keeps zero
/// dependencies). Header values are emitted as JSON strings with every
/// non-printable byte `\u00xx`-escaped; bodies are emitted as hex.
fn describe_request(req: &Request) -> Vec<u8> {
    let mut s = String::new();
    s.push_str("{\"method\":\"");
    json_push(&mut s, req.method.as_bytes());
    s.push_str("\",\"target\":\"");
    json_push(&mut s, req.target.as_bytes());
    s.push_str("\",\"headers\":[");
    push_header_pairs(&mut s, &req.headers);
    s.push_str("],\"framing\":");
    match req.framing {
        Framing::NoBody => s.push_str("{\"mode\":\"none\"}"),
        Framing::ContentLength(n) => {
            s.push_str("{\"mode\":\"content-length\",\"declared_length\":");
            s.push_str(&n.to_string());
            s.push('}');
        }
        Framing::Chunked => s.push_str("{\"mode\":\"chunked\"}"),
    }
    s.push_str(",\"body_hex\":\"");
    push_hex(&mut s, &req.body);
    s.push_str("\",\"body_len\":");
    s.push_str(&req.body.len().to_string());
    s.push_str(",\"trailers\":[");
    push_header_pairs(&mut s, &req.trailers);
    s.push_str("]}\n");
    s.into_bytes()
}

fn push_header_pairs(s: &mut String, headers: &[crate::Header]) {
    for (i, h) in headers.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push_str("[\"");
        json_push(s, h.name.as_bytes());
        s.push_str("\",\"");
        json_push(s, &h.value);
        s.push_str("\"]");
    }
}

fn json_push(s: &mut String, bytes: &[u8]) {
    for &b in bytes {
        match b {
            b'"' => s.push_str("\\\""),
            b'\\' => s.push_str("\\\\"),
            0x20..=0x7E => s.push(b as char),
            _ => s.push_str(&format!("\\u{:04x}", b)),
        }
    }
}

fn push_hex(s: &mut String, bytes: &[u8]) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for &b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xf) as usize] as char);
    }
}
