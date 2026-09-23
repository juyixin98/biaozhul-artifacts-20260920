//! Dependency-free local HTTP verification endpoint.
//!
//! Binary keys/values cross the wire as lowercase hex strings. JSON is emitted
//! and parsed by the tiny helpers in this module — the only accepted request
//! content type is a small JSON object, so a full JSON dependency is overkill.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::coding;
use crate::error::{Error, Result};
use crate::io::PosixReader;
use crate::table::{self, Bound, BuildStats, Options, ScanOptions, Table, ValidationReport};

/// Server configuration and the directory tables live in.
pub struct HttpServer {
    dir: PathBuf,
    listener: TcpListener,
}

impl HttpServer {
    pub fn bind(dir: PathBuf, addr: &str) -> Result<HttpServer> {
        std::fs::create_dir_all(&dir)?;
        let listener = TcpListener::bind(addr)?;
        Ok(HttpServer { dir, listener })
    }

    pub fn local_addr(&self) -> Result<String> {
        Ok(self.listener.local_addr()?.to_string())
    }

    /// Accept connections until the process is killed.
    pub fn serve(self) -> Result<()> {
        let dir = Arc::new(self.dir);
        for stream in self.listener.incoming() {
            match stream {
                Ok(stream) => {
                    let dir = Arc::clone(&dir);
                    thread::spawn(move || {
                        if let Err(e) = handle_connection(stream, &dir) {
                            eprintln!("connection error: {e}");
                        }
                    });
                }
                Err(e) => eprintln!("accept error: {e}"),
            }
        }
        Ok(())
    }
}

fn handle_connection(mut stream: TcpStream, dir: &Path) -> Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(10)))?;
    stream.set_nodelay(true)?;

    let mut req = Request::read(&mut stream)?;
    let response = route(&mut req, dir);
    write_response(&mut stream, &response)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Request / response types
// ---------------------------------------------------------------------------

struct Request {
    method: String,
    path: String,
    query: HashMap<String, String>,
    body: Vec<u8>,
}

impl Request {
    fn read(stream: &mut TcpStream) -> Result<Request> {
        let mut buf = Vec::with_capacity(8192);
        let mut tmp = [0u8; 4096];
        let header_end = loop {
            if let Some(pos) = find_double_crlf(&buf) {
                break pos;
            }
            if buf.len() > 64 * 1024 {
                return Err(Error::invalid_argument("headers too large"));
            }
            let n = stream.read(&mut tmp)?;
            if n == 0 {
                return Err(Error::invalid_argument("connection closed mid-request"));
            }
            buf.extend_from_slice(&tmp[..n]);
        };

        let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
        let mut lines = head.split("\r\n");
        let request_line = lines
            .next()
            .ok_or_else(|| Error::invalid_argument("empty request line"))?;
        let mut parts = request_line.split(' ');
        let method = parts
            .next()
            .ok_or_else(|| Error::invalid_argument("missing method"))?
            .to_string();
        let target = parts
            .next()
            .ok_or_else(|| Error::invalid_argument("missing target"))?;
        let (path, query) = match target.split_once('?') {
            Some((p, q)) => (p.to_string(), parse_query(q)),
            None => (target.to_string(), HashMap::new()),
        };

        let mut content_length = 0usize;
        for line in lines {
            if let Some(rest) = line
                .strip_prefix("Content-Length:")
                .or_else(|| line.strip_prefix("content-length:"))
            {
                content_length = rest
                    .trim()
                    .parse()
                    .map_err(|_| Error::invalid_argument("bad Content-Length"))?;
            }
        }
        if content_length > 64 * 1024 * 1024 {
            return Err(Error::invalid_argument(
                "request body too large (64 MiB cap)",
            ));
        }

        let mut body = buf[header_end + 4..].to_vec();
        while body.len() < content_length {
            let mut more = vec![0u8; (content_length - body.len()).min(4096)];
            let n = stream.read(&mut more)?;
            if n == 0 {
                return Err(Error::invalid_argument("connection closed mid-body"));
            }
            body.extend_from_slice(&more[..n]);
        }
        body.truncate(content_length);

        Ok(Request {
            method,
            path,
            query,
            body,
        })
    }
}

fn find_double_crlf(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn parse_query(q: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for pair in q.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        map.insert(percent_decode(k), percent_decode(v));
    }
    map
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
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
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

struct Response {
    status: u16,
    body: String,
}

impl Response {
    fn json(status: u16, body: String) -> Response {
        Response { status, body }
    }
}

fn status_text(code: u16) -> &'static str {
    match code {
        200 => "OK",
        201 => "Created",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

fn write_response(stream: &mut TcpStream, resp: &Response) -> Result<()> {
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        resp.status,
        status_text(resp.status),
        resp.body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(resp.body.as_bytes())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

fn route(req: &mut Request, dir: &Path) -> Response {
    match dispatch(req, dir) {
        Ok(r) => r,
        Err(e) => {
            let status = match &e {
                Error::InvalidArgument(_) | Error::Corruption(_) => 400,
                Error::Io(io) if io.kind() == std::io::ErrorKind::NotFound => 404,
                Error::Io(io) if io.kind() == std::io::ErrorKind::AlreadyExists => 409,
                Error::Io(_) => 500,
            };
            Response::json(
                status,
                json_object(vec![
                    ("ok", Json::Bool(false)),
                    ("error_kind", Json::s(e.kind())),
                    ("error", Json::s(&e.to_string())),
                ]),
            )
        }
    }
}

fn dispatch(req: &Request, dir: &Path) -> Result<Response> {
    let segments: Vec<&str> = req.path.split('/').filter(|s| !s.is_empty()).collect();

    match (req.method.as_str(), segments.as_slice()) {
        ("GET", ["healthz"]) => Ok(Response::json(
            200,
            json_object(vec![("ok", Json::Bool(true)), ("service", Json::s("psst"))]),
        )),

        ("GET", ["tables"]) => list_tables(dir),

        ("PUT" | "POST", ["tables", name]) => create_table(req, dir, name),
        ("DELETE", ["tables", name]) => delete_table(dir, name),

        ("GET", ["tables", name, "get"]) => get_entry(req, dir, name),
        ("GET", ["tables", name, "scan"]) => scan(req, dir, name),
        ("POST" | "GET", ["tables", name, "validate"]) => validate_table(dir, name),

        _ => Ok(Response::json(
            404,
            json_object(vec![
                ("ok", Json::Bool(false)),
                (
                    "error",
                    Json::s("no such endpoint; see /healthz and README"),
                ),
            ]),
        )),
    }
}

fn table_path(dir: &Path, name: &str) -> Result<PathBuf> {
    table::validate_table_name(name)?;
    Ok(dir.join(name))
}

fn open_table(dir: &Path, name: &str) -> Result<Table<PosixReader>> {
    Table::open_path(&table_path(dir, name)?)
}

fn list_tables(dir: &Path) -> Result<Response> {
    let mut names: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let n = entry.file_name();
        let name = n.to_string_lossy();
        if !name.starts_with('.') {
            names.push(name.into_owned());
        }
    }
    names.sort();
    let arr: Vec<Json> = names.iter().map(|n| Json::s(n)).collect();
    Ok(Response::json(
        200,
        json_object(vec![("ok", Json::Bool(true)), ("tables", Json::Arr(arr))]),
    ))
}

fn create_table(req: &Request, dir: &Path, name: &str) -> Result<Response> {
    table::validate_table_name(name)?;
    let parsed = ParseBody::parse(&req.body)?;
    let mut options = Options::default();
    if let Some(v) = parsed.field("block_size") {
        options.block_size = v.as_usize()?;
    }
    if let Some(v) = parsed.field("restart_interval") {
        options.restart_interval = v.as_u64()? as u32;
    }

    let entries_json = parsed
        .field("entries")
        .ok_or_else(|| Error::invalid_argument("missing `entries` array"))?;
    let pairs = entries_json.as_pairs_hex()?;
    if pairs.is_empty() {
        return Err(Error::invalid_argument(
            "refusing to build a table with zero entries",
        ));
    }

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
        ^ std::process::id() as u64;
    let stats: BuildStats = table::build_table_sorted(dir, name, &pairs, options, unique)?;

    Ok(Response::json(
        201,
        json_object(vec![
            ("ok", Json::Bool(true)),
            ("table", Json::s(name)),
            ("bytes", Json::Num(stats.bytes)),
            ("entries", Json::Num(stats.entries)),
            ("data_blocks", Json::Num(stats.data_blocks)),
        ]),
    ))
}

fn delete_table(dir: &Path, name: &str) -> Result<Response> {
    table::remove_table_file(dir, name)?;
    Ok(Response::json(
        200,
        json_object(vec![("ok", Json::Bool(true)), ("deleted", Json::s(name))]),
    ))
}

fn get_hex_param(req: &Request, key: &str) -> Result<Option<Vec<u8>>> {
    match req.query.get(key) {
        Some(raw) => Ok(Some(coding::from_hex(raw)?)),
        None => Ok(None),
    }
}

fn get_entry(req: &Request, dir: &Path, name: &str) -> Result<Response> {
    let table = open_table(dir, name)?;
    let key = get_hex_param(req, "key")?
        .ok_or_else(|| Error::invalid_argument("missing `key` hex query parameter"))?;
    match table.get(&key)? {
        Some(value) => Ok(Response::json(
            200,
            json_object(vec![
                ("ok", Json::Bool(true)),
                ("found", Json::Bool(true)),
                ("key", Json::s(&coding::to_hex(&key))),
                ("value", Json::s(&coding::to_hex(&value))),
            ]),
        )),
        None => Ok(Response::json(
            200,
            json_object(vec![
                ("ok", Json::Bool(true)),
                ("found", Json::Bool(false)),
                ("key", Json::s(&coding::to_hex(&key))),
            ]),
        )),
    }
}

fn scan(req: &Request, dir: &Path, name: &str) -> Result<Response> {
    let table = open_table(dir, name)?;

    let start = match get_hex_param(req, "start")? {
        Some(k) => {
            if req.query.contains_key("start_exclusive") {
                Bound::Excluded(k)
            } else {
                Bound::Included(k)
            }
        }
        None => Bound::Unbounded,
    };
    let end = match get_hex_param(req, "end")? {
        Some(k) => {
            if req.query.contains_key("end_inclusive") {
                Bound::Included(k)
            } else {
                Bound::Excluded(k)
            }
        }
        None => Bound::Unbounded,
    };
    let limit = match req.query.get("limit") {
        Some(s) => Some(
            s.parse::<u64>()
                .map_err(|_| Error::invalid_argument("bad `limit`"))?,
        ),
        None => None,
    };

    let mut iter = table.scan(ScanOptions { start, end, limit })?;
    let mut rows: Vec<Json> = Vec::new();
    while let Some((k, v)) = iter.next_entry()? {
        rows.push(Json::Arr(vec![
            Json::owned_str(coding::to_hex(&k)),
            Json::owned_str(coding::to_hex(&v)),
        ]));
    }
    Ok(Response::json(
        200,
        json_object(vec![
            ("ok", Json::Bool(true)),
            ("count", Json::Num(rows.len() as u64)),
            ("entries", Json::Arr(rows)),
        ]),
    ))
}

fn validate_table(dir: &Path, name: &str) -> Result<Response> {
    let table = open_table(dir, name)?;
    let report: ValidationReport = table.validate()?;
    let blocks: Vec<Json> = report
        .data_blocks
        .iter()
        .map(|b| {
            Json::Obj(vec![
                ("offset".to_string(), Json::Num(b.offset)),
                ("payload_size".to_string(), Json::Num(b.payload_size)),
                ("entries".to_string(), Json::Num(b.entries as u64)),
                ("restarts".to_string(), Json::Num(b.restarts as u64)),
            ])
        })
        .collect();
    Ok(Response::json(
        200,
        json_object(vec![
            ("ok", Json::Bool(true)),
            ("valid", Json::Bool(true)),
            ("file_size", Json::Num(report.file_size)),
            ("total_entries", Json::Num(report.total_entries as u64)),
            ("total_restarts", Json::Num(report.total_restarts as u64)),
            ("data_blocks", Json::Arr(blocks)),
        ]),
    ))
}

// ---------------------------------------------------------------------------
// Minimal JSON
// ---------------------------------------------------------------------------

enum Json {
    Bool(bool),
    Num(u64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    fn s(s: &str) -> Json {
        Json::Str(s.to_string())
    }
    fn owned_str(s: String) -> Json {
        Json::Str(s)
    }
}

fn json_object(fields: Vec<(&str, Json)>) -> String {
    let value = Json::Obj(
        fields
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
    );
    let mut s = String::new();
    write_json(&value, &mut s);
    s
}

// The `Json` value type is only used for emitting responses; the request
// parser produces the separate, body-borrowing `JsonRef` tree below.

fn write_json(v: &Json, out: &mut String) {
    match v {
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Num(n) => out.push_str(&n.to_string()),
        Json::Str(s) => write_json_string(s, out),
        Json::Arr(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json(item, out);
            }
            out.push(']');
        }
        Json::Obj(o) => {
            out.push('{');
            for (i, (k, v)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(k, out);
                out.push(':');
                write_json(v, out);
            }
            out.push('}');
        }
    }
}

fn write_json_string(s: &str, out: &mut String) {
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
}

// ---------------------------------------------------------------------------
// Tiny JSON *parser* (sufficient for the build endpoint's request bodies)
// ---------------------------------------------------------------------------

struct ParseBody<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> ParseBody<'a> {
    fn parse(bytes: &'a [u8]) -> Result<ParseBody<'a>> {
        let mut p = ParseBody { bytes, pos: 0 };
        p.skip_ws();
        let _ = p.parse_value()?;
        p.skip_ws();
        if p.pos != bytes.len() {
            return Err(Error::invalid_argument("trailing bytes after JSON body"));
        }
        p.pos = 0;
        p.skip_ws();
        Ok(p)
    }

    fn field(&self, name: &str) -> Option<JsonRef<'a>> {
        let mut p = ParseBody {
            bytes: self.bytes,
            pos: 0,
        };
        p.skip_ws();
        let v = p.parse_value().ok()?;
        if let JsonRef::Obj(o) = v {
            for (k, val) in o {
                if k == name {
                    return Some(val);
                }
            }
        }
        None
    }

    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.pos += 1;
        }
    }

    fn parse_value(&mut self) -> Result<JsonRef<'a>> {
        self.skip_ws();
        if self.pos >= self.bytes.len() {
            return Err(Error::invalid_argument("unexpected end of JSON"));
        }
        match self.bytes[self.pos] {
            b'{' => self.parse_object(),
            b'[' => self.parse_array(),
            b'"' => Ok(JsonRef::Str(self.parse_string()?)),
            b't' | b'f' => self.parse_bool(),
            b'n' => self.parse_null(),
            b'-' | b'0'..=b'9' => self.parse_number(),
            c => Err(Error::invalid_argument(format!(
                "unexpected byte {c:#x} in JSON"
            ))),
        }
    }

    fn parse_object(&mut self) -> Result<JsonRef<'a>> {
        self.pos += 1;
        let mut entries = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonRef::Obj(entries));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            self.expect(b':')?;
            let val = self.parse_value()?;
            entries.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(Error::invalid_argument("malformed JSON object")),
            }
        }
        Ok(JsonRef::Obj(entries))
    }

    fn parse_array(&mut self) -> Result<JsonRef<'a>> {
        self.pos += 1;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(JsonRef::Arr(items));
        }
        loop {
            items.push(self.parse_value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(Error::invalid_argument("malformed JSON array")),
            }
        }
        Ok(JsonRef::Arr(items))
    }

    fn parse_string(&mut self) -> Result<&'a str> {
        self.expect(b'"')?;
        let start = self.pos;
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b'"' => {
                    let s = std::str::from_utf8(&self.bytes[start..self.pos])
                        .map_err(|_| Error::invalid_argument("JSON string is not UTF-8"))?;
                    self.pos += 1;
                    return Ok(s);
                }
                b'\\' => {
                    return Err(Error::invalid_argument(
                        "escaped JSON strings are not accepted in this API",
                    ))
                }
                c if c < 0x20 => {
                    return Err(Error::invalid_argument(
                        "unescaped control byte in JSON string",
                    ))
                }
                _ => self.pos += 1,
            }
        }
        Err(Error::invalid_argument("unterminated JSON string"))
    }

    fn parse_bool(&mut self) -> Result<JsonRef<'a>> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(JsonRef::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(JsonRef::Bool(false))
        } else {
            Err(Error::invalid_argument("bad JSON literal"))
        }
    }

    fn parse_null(&mut self) -> Result<JsonRef<'a>> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(JsonRef::Null)
        } else {
            Err(Error::invalid_argument("bad JSON literal"))
        }
    }

    fn parse_number(&mut self) -> Result<JsonRef<'a>> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        // Accept non-negative integers only (reject fraction/exponent).
        if matches!(self.peek(), Some(b'.') | Some(b'e') | Some(b'E')) {
            return Err(Error::invalid_argument("only integers are accepted"));
        }
        let s = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| Error::invalid_argument("bad JSON number"))?;
        let n: u64 = s
            .parse()
            .map_err(|_| Error::invalid_argument("invalid or negative integer"))?;
        Ok(JsonRef::OwnedNum(n))
    }

    fn expect(&mut self, b: u8) -> Result<()> {
        self.skip_ws();
        if self.peek() == Some(b) {
            self.pos += 1;
            Ok(())
        } else {
            Err(Error::invalid_argument(format!("expected {b:#x} in JSON")))
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }
}

/// Borrowed parse tree (strings/objects borrow the request body). The Bool and
/// Null payloads are never queried — the API only reads ints/strings/arrays —
/// but the variants must exist so those literals parse without an error.
#[allow(dead_code)]
enum JsonRef<'a> {
    Null,
    Bool(bool),
    OwnedNum(u64),
    Str(&'a str),
    Arr(Vec<JsonRef<'a>>),
    Obj(Vec<(&'a str, JsonRef<'a>)>),
}

impl<'a> JsonRef<'a> {
    fn as_u64(&self) -> Result<u64> {
        match self {
            JsonRef::OwnedNum(n) => Ok(*n),
            _ => Err(Error::invalid_argument("expected an integer")),
        }
    }

    fn as_usize(&self) -> Result<usize> {
        let n = self.as_u64()?;
        if n > usize::MAX as u64 {
            return Err(Error::invalid_argument("integer too large"));
        }
        Ok(n as usize)
    }

    fn as_pairs_hex(&self) -> Result<Vec<(Vec<u8>, Vec<u8>)>> {
        let arr = match self {
            JsonRef::Arr(a) => a,
            _ => return Err(Error::invalid_argument("`entries` must be an array")),
        };
        let mut out = Vec::with_capacity(arr.len());
        for item in arr {
            let pair = match item {
                JsonRef::Arr(p) if p.len() == 2 => p,
                _ => {
                    return Err(Error::invalid_argument(
                        "each entry must be a [key_hex, value_hex] pair",
                    ))
                }
            };
            let k = match &pair[0] {
                JsonRef::Str(s) => coding::from_hex(s)?,
                _ => return Err(Error::invalid_argument("key must be a hex string")),
            };
            let v = match &pair[1] {
                JsonRef::Str(s) => coding::from_hex(s)?,
                _ => return Err(Error::invalid_argument("value must be a hex string")),
            };
            out.push((k, v));
        }
        Ok(out)
    }
}
