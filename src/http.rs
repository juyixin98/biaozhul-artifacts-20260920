//! Minimal dependency-free HTTP/1.1 layer.
//!
//! One request per connection (`Connection: close`), bounded request body,
//! simple path/query routing. No third-party runtime — each accepted
//! connection is served on its own OS thread.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::time::Duration;

pub const MAX_BODY: usize = 64 * 1024 * 1024;

#[derive(Debug)]
pub struct Request {
    pub method: String,
    pub path: String,  // path component, no query
    pub query: String, // raw query string after '?'
    pub body: Vec<u8>,
}

impl Request {
    pub fn query_param(&self, name: &str) -> Option<String> {
        for pair in self.query.split('&').filter(|p| !p.is_empty()) {
            let mut it = pair.splitn(2, '=');
            let k = it.next().unwrap_or("");
            if k == name {
                let v = it.next().unwrap_or("");
                return Some(percent_decode(v));
            }
        }
        None
    }
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hi = (bytes[i + 1] as char).to_digit(16);
                let lo = (bytes[i + 2] as char).to_digit(16);
                if let (Some(hi), Some(lo)) = (hi, lo) {
                    out.push((hi * 16 + lo) as u8);
                    i += 3;
                } else {
                    out.push(bytes[i]);
                    i += 1;
                }
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

#[derive(Debug)]
pub enum HttpError {
    BadRequest(&'static str),
    EntityTooLarge,
}

fn read_request(stream: &mut TcpStream) -> Result<Request, HttpError> {
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|_| HttpError::BadRequest("timeout setup failed"))?;

    let mut buf = Vec::with_capacity(4096);
    let mut tmp = [0u8; 4096];
    let header_end = loop {
        let n = stream
            .read(&mut tmp)
            .map_err(|_| HttpError::BadRequest("read error"))?;
        if n == 0 {
            return Err(HttpError::BadRequest("connection closed before request"));
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break pos + 4;
        }
        if buf.len() > 1024 * 1024 {
            return Err(HttpError::BadRequest("headers too large"));
        }
    };

    let head = String::from_utf8_lossy(&buf[..header_end]).into_owned();
    let mut lines = head.split("\r\n");
    let request_line = lines.next().ok_or(HttpError::BadRequest("empty request"))?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().ok_or(HttpError::BadRequest("no method"))?;
    let target = parts.next().ok_or(HttpError::BadRequest("no target"))?;
    let version = parts.next().ok_or(HttpError::BadRequest("no version"))?;
    if version != "HTTP/1.1" && version != "HTTP/1.0" {
        return Err(HttpError::BadRequest("unsupported HTTP version"));
    }

    let mut content_length = 0usize;
    for line in lines {
        if line.is_empty() {
            break;
        }
        let colon = line.find(':').ok_or(HttpError::BadRequest("bad header"))?;
        let name = line[..colon].trim().to_ascii_lowercase();
        let value = line[colon + 1..].trim();
        if name == "content-length" {
            content_length = value
                .parse::<usize>()
                .map_err(|_| HttpError::BadRequest("bad content-length"))?;
        }
        if name == "transfer-encoding" && value.to_ascii_lowercase().contains("chunked") {
            return Err(HttpError::BadRequest(
                "chunked transfer encoding not supported",
            ));
        }
    }
    if content_length > MAX_BODY {
        return Err(HttpError::EntityTooLarge);
    }

    let mut body = buf[header_end..].to_vec();
    while body.len() < content_length {
        let n = stream
            .read(&mut tmp)
            .map_err(|_| HttpError::BadRequest("body read error"))?;
        if n == 0 {
            return Err(HttpError::BadRequest("truncated body"));
        }
        body.extend_from_slice(&tmp[..n]);
    }
    body.truncate(content_length);

    let (path, query) = match target.find('?') {
        Some(i) => (target[..i].to_string(), target[i + 1..].to_string()),
        None => (target.to_string(), String::new()),
    };
    Ok(Request {
        method: method.to_string(),
        path,
        query,
        body,
    })
}

pub struct Response {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(status: u16, body: impl Into<Vec<u8>>) -> Self {
        Response {
            status,
            content_type: "application/json".to_string(),
            body: body.into(),
        }
    }

    pub fn error_json(status: u16, message: &str) -> Self {
        let text = crate::json::obj(vec![
            ("error", crate::json::Json::Str(message.to_string())),
            ("status", crate::json::Json::Int(status as i64)),
        ]);
        Self::json(status, text.pretty())
    }
}

fn status_text(code: u16) -> &'static str {
    match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        413 => "Payload Too Large",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

pub fn write_response(stream: &mut TcpStream, resp: &Response) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        resp.status,
        status_text(resp.status),
        resp.content_type,
        resp.body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(&resp.body)?;
    stream.flush()
}

pub trait Handler: Send + Sync {
    fn handle(&self, req: &Request) -> Response;
}

pub fn serve<H: Handler + 'static>(listener: TcpListener, handler: Arc<H>) -> std::io::Result<()> {
    listener.set_nonblocking(false)?;
    for stream in listener.incoming() {
        match stream {
            Ok(mut stream) => {
                let h = Arc::clone(&handler);
                std::thread::spawn(move || {
                    let resp = match read_request(&mut stream) {
                        Ok(req) => h.handle(&req),
                        Err(HttpError::EntityTooLarge) => {
                            Response::error_json(413, "body too large")
                        }
                        Err(HttpError::BadRequest(msg)) => Response::error_json(400, msg),
                    };
                    let _ = write_response(&mut stream, &resp);
                });
            }
            Err(_) => continue,
        }
    }
    Ok(())
}
