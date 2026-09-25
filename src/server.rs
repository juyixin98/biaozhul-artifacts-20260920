//! Local TCP test service built directly on the incremental framer.
//!
//! This is *not* a proxy: it terminates each connection, frames the request
//! stream itself, replies with a JSON description of every parsed frame, and
//! closes the connection on the first framing error (400/413). Pipelined
//! requests on one connection each produce an in-order JSON response.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

use crate::error::ParseError;
use crate::framer::{Framer, Limits, RequestFrame, Step};

/// Service configuration.
#[derive(Debug, Clone, Copy)]
pub struct ServerConfig {
    pub limits: Limits,
    /// Idle timeout: a connection sending nothing (or stopping mid-request)
    /// is dropped after this duration.
    pub idle_timeout: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            limits: Limits::default(),
            idle_timeout: Duration::from_secs(5),
        }
    }
}

/// Bind `addr` and serve until the listener errors.
///
/// The bound address is written to stderr (`listening on …`) so callers that
/// bind port 0 can discover the chosen port.
pub fn serve(addr: &str, config: ServerConfig) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    eprintln!("listening on {}", listener.local_addr()?);
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                std::thread::spawn(move || {
                    let _ = handle_connection(stream, config);
                });
            }
            Err(err) => eprintln!("accept error: {err}"),
        }
    }
    Ok(())
}

/// Run one framed connection. All I/O errors are surfaced to the caller;
/// framing errors produce an HTTP error response and a clean return.
pub fn handle_connection(mut stream: TcpStream, config: ServerConfig) -> std::io::Result<()> {
    stream.set_read_timeout(Some(config.idle_timeout))?;
    stream.set_nodelay(true)?;

    let mut framer = Framer::with_limits(config.limits);
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 8192];
    let mut pos = 0usize; // unconsumed prefix index inside `buf`
    let mut seq = 0u64;

    loop {
        // Drain as many frames / errors as the buffer allows.
        while pos < buf.len() {
            match framer.step(&buf[pos..]) {
                (Step::Frame(frame), used) => {
                    pos += used;
                    seq += 1;
                    write_frame_response(&mut stream, seq, &frame)?;
                    stream.flush()?;
                    if !frame.keep_alive {
                        return Ok(());
                    }
                }
                (Step::Incomplete, used) => {
                    pos += used;
                    break;
                }
                (Step::Error(err), _used) => {
                    write_error_response(&mut stream, err)?;
                    stream.flush()?;
                    return Ok(());
                }
            }
        }

        // Drop bytes already consumed.
        if pos > 0 {
            buf.drain(..pos);
            pos = 0;
        }

        match stream.read(&mut chunk) {
            Ok(0) => return Ok(()), // client half-close / EOF
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(err)
                if err.kind() == std::io::ErrorKind::WouldBlock
                    || err.kind() == std::io::ErrorKind::TimedOut =>
            {
                return Ok(())
            }
            Err(err) => return Err(err),
        }
    }
}

fn write_frame_response(
    out: &mut impl Write,
    seq: u64,
    frame: &RequestFrame,
) -> std::io::Result<()> {
    let body = frame_json(seq, frame);
    let conn = if frame.keep_alive {
        "keep-alive"
    } else {
        "close"
    };
    write!(
        out,
        "HTTP/1.1 200 OK\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: {conn}\r\n\
         \r\n",
        body.len(),
    )?;
    out.write_all(body.as_bytes())
}

fn write_error_response(out: &mut impl Write, err: ParseError) -> std::io::Result<()> {
    let status = err.kind.http_status();
    let reason = if status == 413 {
        "Payload Too Large"
    } else {
        "Bad Request"
    };
    let body = format!(
        "{{\"type\":\"error\",\"status\":{status},\"error\":\"{}\",\"offset\":{}}}",
        err.kind.code(),
        err.offset,
    );
    write!(
        out,
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\
         \r\n",
        body.len(),
    )?;
    out.write_all(body.as_bytes())
}

fn frame_json(seq: u64, frame: &RequestFrame) -> String {
    let (framing, content_length) = match frame.framing {
        crate::framer::Framing::None => ("none", None),
        crate::framer::Framing::FixedLen(n) => ("fixed-length", Some(n)),
        crate::framer::Framing::Chunked => ("chunked", None),
    };

    let mut s = String::new();
    s.push_str("{\"type\":\"frame\",\"seq\":");
    s.push_str(&seq.to_string());
    s.push_str(",\"method\":\"");
    json_escape_into(&mut s, &frame.method);
    s.push_str("\",\"target\":\"");
    json_escape_into(&mut s, &frame.target);
    s.push_str("\",\"framing\":\"");
    s.push_str(framing);
    s.push('"');
    if let Some(n) = content_length {
        s.push_str(",\"content_length\":");
        s.push_str(&n.to_string());
    }
    s.push_str(",\"header_count\":");
    s.push_str(&frame.headers.len().to_string());
    s.push_str(",\"headers\":[");
    for (i, h) in frame.headers.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push_str("[\"");
        json_escape_into(&mut s, &h.name);
        s.push_str("\",\"");
        json_escape_into(&mut s, &h.value);
        s.push_str("\"]");
    }
    s.push_str("],\"body_len\":");
    s.push_str(&frame.body.len().to_string());
    s.push_str(",\"body\":\"");
    json_escape_into(&mut s, &frame.body);
    s.push_str("\",\"trailer_count\":");
    s.push_str(&frame.trailers.len().to_string());
    s.push_str(",\"trailers\":[");
    for (i, h) in frame.trailers.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push_str("[\"");
        json_escape_into(&mut s, &h.name);
        s.push_str("\",\"");
        json_escape_into(&mut s, &h.value);
        s.push_str("\"]");
    }
    s.push_str("],\"consumed\":");
    s.push_str(&frame.consumed.to_string());
    s.push('}');
    s
}

/// Minimal JSON string escaper: the protocol is byte oriented, so arbitrary
/// (possibly non-UTF-8) payloads are emitted as `\\uXXXX`-escaped UTF-16
/// rather than forwarded raw.
fn json_escape_into(out: &mut String, bytes: &[u8]) {
    use std::fmt::Write as _;
    let lossy = String::from_utf8_lossy(bytes);
    for c in lossy.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
}
