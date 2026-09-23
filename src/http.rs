//! Zero-dependency local HTTP/1.1 validation entrypoint (stdlib only).
//!
//! Exposes the real file-backed store under `/kv*`, plus deterministic
//! crash-injection demos under `/demo*` (these run entirely on the in-memory
//! [`SimVfs`], so they never touch the real data directory).

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

use crate::demo::{self, CaseResult};
use crate::io::{CrashPolicy, RealVfs, Torn};
use crate::json::Json;
use crate::repo::{Repository, StoreError};

pub struct ServerState {
    pub store: Mutex<Repository<RealVfs>>,
}

pub fn serve(dir: &str, addr: &str) -> std::io::Result<()> {
    let vfs = RealVfs::open(dir).map_err(io_err)?;
    let (store, report) = Repository::open(vfs).map_err(|e| std::io::Error::other(e.to_string()))?;
    let state = Arc::new(ServerState {
        store: Mutex::new(store),
    });

    eprintln!("data directory: {dir}");
    eprintln!("recovery: selected {:?} gen {:?}, data {} bytes ({} orphan byte(s) truncated)",
        report.selected_slot,
        report.selected_generation,
        report.data_len_after,
        report.truncated_orphan_bytes);
    for s in &report.slots {
        eprintln!(
            "  slot {} present={} accepted={}: {}",
            s.slot, s.present, s.accepted, s.detail
        );
    }
    let listener = TcpListener::bind(addr)?;
    eprintln!("listening on http://{addr}");
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let state = Arc::clone(&state);
                // Threads per connection are fine for a local validation tool.
                std::thread::spawn(move || {
                    if let Err(e) = handle_connection(stream, &state) {
                        eprintln!("connection error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

fn io_err(e: crate::io::IoError) -> std::io::Error {
    std::io::Error::other(e.to_string())
}

// ---------------------------------------------------------------------------
// Connection / routing
// ---------------------------------------------------------------------------

fn handle_connection(mut stream: TcpStream, state: &Arc<ServerState>) -> std::io::Result<()> {
    stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
    let mut buf = [0u8; 64 * 1024];
    let mut total = 0usize;
    let header_end;
    loop {
        let n = stream.read(&mut buf[total..])?;
        if n == 0 {
            return Ok(());
        }
        total += n;
        if let Some(pos) = find_subsequence(&buf[..total], b"\r\n\r\n") {
            header_end = pos;
            break;
        }
        if total == buf.len() {
            return Ok(()); // headers too large; drop
        }
    }
    let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split(' ');
    let method = parts.next().unwrap_or("");
    let target = parts.next().unwrap_or("");
    let (path, query) = target.split_once('?').unwrap_or((target, ""));

    // Body: Content-Length bytes following the header terminator.
    let content_length = head
        .split("\r\n")
        .find_map(|l| {
            let (k, v) = l.split_once(':')?;
            if k.eq_ignore_ascii_case("content-length") {
                v.trim().parse::<usize>().ok()
            } else {
                None
            }
        })
        .unwrap_or(0);
    let mut body = buf[header_end + 4..total].to_vec();
    while body.len() < content_length {
        let mut tmp = [0u8; 8192];
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    body.truncate(content_length);

    let resp = route(method, path, query, &body, state);
    write_response(&mut stream, resp)
}

struct Response {
    status: u16,
    json: Json,
}

fn route(method: &str, path: &str, query: &str, body: &[u8], state: &Arc<ServerState>) -> Response {
    match (method, path) {
        ("GET", "/health") => ok(Json::Obj(vec![
            ("status".into(), Json::s("ok")),
            ("service".into(), Json::s("dual-sb-recovery")),
        ])),

        ("GET", "/kv") => {
            let store = state.store.lock().unwrap();
            let entries = store
                .entries()
                .into_iter()
                .map(|(k, v)| {
                    Json::Obj(vec![("key".into(), Json::s(k)), ("value".into(), Json::s(v))])
                })
                .collect();
            ok(Json::Obj(vec![
                ("generation".into(), Json::n(store.generation())),
                ("data_len".into(), Json::n(store.data_len())),
                ("entries".into(), Json::Arr(entries)),
            ]))
        }

        ("GET", "/kv/get") => {
            let params = parse_query(query);
            match params.get("key") {
                Some(key) => {
                    let store = state.store.lock().unwrap();
                    match store.get(key.as_bytes()) {
                        Some(v) => ok(Json::Obj(vec![
                            ("key".into(), Json::s(key.clone())),
                            ("value".into(), Json::s(String::from_utf8_lossy(v).into_owned())),
                        ])),
                        None => err404("key not found"),
                    }
                }
                None => err400("missing ?key="),
            }
        }

        ("POST", "/kv/put") => match parse_put_body(body) {
            Ok((key, value)) => {
                let mut store = state.store.lock().unwrap();
                match store.put(key.into_bytes(), value.into_bytes()) {
                    Ok(gen) => ok(Json::Obj(vec![
                        ("committed_generation".into(), Json::n(gen)),
                        ("data_len".into(), Json::n(store.data_len())),
                    ])),
                    Err(e) => err500(&e),
                }
            }
            Err(msg) => err400(&msg),
        },

        ("POST", "/kv/delete") => match parse_delete_body(body, query) {
            Ok(key) => {
                let mut store = state.store.lock().unwrap();
                match store.delete(key.into_bytes()) {
                    Ok(gen) => ok(Json::Obj(vec![
                        ("committed_generation".into(), Json::n(gen)),
                    ])),
                    Err(e) => err500(&e),
                }
            }
            Err(msg) => err400(&msg),
        },

        ("GET", "/demo/matrix") => {
            let cases = demo::run_matrix();
            ok(matrix_json(&cases, "crash during commit 3: 8 points x 3 torn variants"))
        }

        ("GET", "/demo/matrix2") => {
            let cases = demo::run_round2_matrix();
            ok(matrix_json(&cases, "crash during commit 2 (first rollback): half-page torn"))
        }

        ("POST", "/demo/crash") => {
            // body: {"point":"RootWriteEnd","torn":"Half","round":3}
            let parsed = parse_json_object(body);
            let point = parsed
                .get("point")
                .and_then(|v| v.as_str())
                .and_then(demo::parse_point);
            let torn = parsed
                .get("torn")
                .and_then(|v| v.as_str())
                .and_then(demo::parse_torn)
                .unwrap_or(Torn::Half);
            let rounds = parsed
                .get("round")
                .and_then(|v| v.as_u64())
                .unwrap_or(3)
                .clamp(1, 100);
            match point {
                Some(point) => {
                    let case = demo::run_single(CrashPolicy::new(point, torn), rounds);
                    ok(case_json(&case))
                }
                None => err400("body must be {\"point\": <CrashPoint>, \"torn\": None|Half|Short, \"round\": N}"),
            }
        }

        ("GET", "/demo/scenarios") => {
            let named = demo::all_named();
            let arr = named
                .iter()
                .map(|s| {
                    Json::Obj(vec![
                        ("name".into(), Json::s(s.name.clone())),
                        ("description".into(), Json::s(s.description.clone())),
                        ("outcome".into(), Json::s(s.outcome.clone())),
                        ("report".into(), s.report.clone()),
                    ])
                })
                .collect();
            ok(Json::Obj(vec![("scenarios".into(), Json::Arr(arr))]))
        }

        _ => Response {
            status: 404,
            json: Json::Obj(vec![("error".into(), Json::s("no such route") )]),
        },
    }
}

fn matrix_json(cases: &[CaseResult], title: &str) -> Json {
    let total = cases.len() as u64;
    let passed = cases.iter().filter(|c| c.passed()).count() as u64;
    let arr = cases.iter().map(case_json_min).collect();
    Json::Obj(vec![
        ("title".into(), Json::s(title)),
        ("cases".into(), Json::n(total)),
        ("passed".into(), Json::n(passed)),
        ("all_passed".into(), Json::Bool(passed == total)),
        ("results".into(), Json::Arr(arr)),
    ])
}

fn case_json_min(c: &CaseResult) -> Json {
    Json::Obj(vec![
        ("point".into(), Json::s(c.point)),
        ("torn".into(), Json::s(c.torn)),
        ("recovered_generation".into(), optn(c.recovered_generation)),
        ("expected_generation".into(), Json::n(c.expected_generation)),
        ("value".into(), match &c.last_value {
            Some(v) => Json::s(v),
            None => Json::Null,
        }),
        ("orphan_bytes_truncated".into(), Json::n(c.orphan_bytes_truncated)),
        ("passed".into(), Json::Bool(c.passed())),
    ])
}

fn case_json(c: &CaseResult) -> Json {
    let slots = c
        .slot_reports
        .iter()
        .map(|(slot, present, accepted, detail)| {
            Json::Obj(vec![
                ("slot".into(), Json::n(*slot as u64)),
                ("present".into(), Json::Bool(*present)),
                ("accepted".into(), Json::Bool(*accepted)),
                ("detail".into(), Json::s(detail.clone())),
            ])
        })
        .collect();
    let mut obj = vec![
        ("crashed_round".into(), Json::n(c.crashed_round)),
        ("point".into(), Json::s(c.point)),
        ("torn".into(), Json::s(c.torn)),
        ("opened".into(), Json::Bool(c.opened)),
        ("recovered_generation".into(), optn(c.recovered_generation)),
        ("expected_generation".into(), Json::n(c.expected_generation)),
        ("orphan_bytes_truncated".into(), Json::n(c.orphan_bytes_truncated)),
        ("slots".into(), Json::Arr(slots)),
        ("passed".into(), Json::Bool(c.passed())),
    ];
    if let Some(e) = &c.error {
        obj.push(("error".into(), Json::s(e.clone())));
    }
    Json::Obj(obj)
}

fn optn(v: Option<u64>) -> Json {
    match v {
        Some(n) => Json::n(n),
        None => Json::Null,
    }
}

// ---------------------------------------------------------------------------
// Tiny JSON parser (flat objects + strings/numbers/null/bool), enough for
// the request bodies this service accepts.
// ---------------------------------------------------------------------------

pub fn parse_json_object(body: &[u8]) -> BTreeMap<String, Json> {
    let mut out = BTreeMap::new();
    let s = std::str::from_utf8(body).unwrap_or("");
    let bytes = s.as_bytes();
    let mut i = skip_ws(bytes, 0);
    if i >= bytes.len() || bytes[i] != b'{' {
        return out;
    }
    i += 1;
    loop {
        i = skip_ws(bytes, i);
        if i >= bytes.len() {
            break;
        }
        if bytes[i] == b'}' {
            break;
        }
        if bytes[i] != b'"' {
            break;
        }
        let (key, ni) = match read_json_string(bytes, i) {
            Some(x) => x,
            None => break,
        };
        i = skip_ws(bytes, ni);
        if i >= bytes.len() || bytes[i] != b':' {
            break;
        }
        i += 1;
        i = skip_ws(bytes, i);
        let (val, ni) = match read_json_value(bytes, i) {
            Some(x) => x,
            None => break,
        };
        out.insert(key, val);
        i = skip_ws(bytes, ni);
        if i < bytes.len() && bytes[i] == b',' {
            i += 1;
            continue;
        }
        if i < bytes.len() && bytes[i] == b'}' {
            break;
        }
        break;
    }
    out
}

fn skip_ws(b: &[u8], mut i: usize) -> usize {
    while i < b.len() && matches!(b[i], b' ' | b'\t' | b'\n' | b'\r') {
        i += 1;
    }
    i
}

fn read_json_string(b: &[u8], start: usize) -> Option<(String, usize)> {
    if b[start] != b'"' {
        return None;
    }
    let mut i = start + 1;
    let mut out = String::new();
    while i < b.len() {
        match b[i] {
            b'"' => return Some((out, i + 1)),
            b'\\' if i + 1 < b.len() => {
                match b[i + 1] {
                    b'n' => out.push('\n'),
                    b't' => out.push('\t'),
                    b'r' => out.push('\r'),
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    _ => out.push(b[i + 1] as char),
                }
                i += 2;
            }
            c => {
                // copy a UTF-8 run
                let width = utf8_width(c);
                if i + width > b.len() {
                    return None;
                }
                out.push_str(std::str::from_utf8(&b[i..i + width]).ok()?);
                i += width;
            }
        }
    }
    None
}

fn utf8_width(c: u8) -> usize {
    if c < 0x80 { 1 } else if c >> 5 == 0b110 { 2 } else if c >> 4 == 0b1110 { 3 } else if c >> 3 == 0b11110 { 4 } else { 1 }
}

fn read_json_value(b: &[u8], start: usize) -> Option<(Json, usize)> {
    let i = skip_ws(b, start);
    if i >= b.len() {
        return None;
    }
    match b[i] {
        b'"' => read_json_string(b, i).map(|(s, n)| (Json::s(s), n)),
        b't' if b[i..].starts_with(b"true") => Some((Json::Bool(true), i + 4)),
        b'f' if b[i..].starts_with(b"false") => Some((Json::Bool(false), i + 5)),
        b'n' if b[i..].starts_with(b"null") => Some((Json::Null, i + 4)),
        b'0'..=b'9' | b'-' => {
            let mut j = i;
            if b[j] == b'-' {
                j += 1;
            }
            while j < b.len() && (b[j].is_ascii_digit() || b[j] == b'.') {
                j += 1;
            }
            let n: f64 = std::str::from_utf8(&b[i..j]).ok()?.parse().ok()?;
            Some((Json::Num(n), j))
        }
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// request helpers
// ---------------------------------------------------------------------------

fn parse_put_body(body: &[u8]) -> Result<(String, String), String> {
    let o = parse_json_object(body);
    let key = o.get("key").and_then(|v| v.as_str()).ok_or("body must contain \"key\"")?;
    let value = o.get("value").and_then(|v| v.as_str()).ok_or("body must contain \"value\"")?;
    Ok((key.to_string(), value.to_string()))
}

fn parse_delete_body(body: &[u8], query: &str) -> Result<String, String> {
    if let Some(k) = parse_query(query).remove("key") {
        return Ok(k);
    }
    let o = parse_json_object(body);
    o.get("key")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or("missing key (JSON body {\"key\":...} or ?key=)".to_string())
}

fn parse_query(query: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for pair in query.split('&').filter(|p| !p.is_empty()) {
        if let Some((k, v)) = pair.split_once('=') {
            out.insert(percent_decode(k), percent_decode(v));
        }
    }
    out
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let h = |c: u8| match c {
                    b'0'..=b'9' => Some(c - b'0'),
                    b'a'..=b'f' => Some(c - b'a' + 10),
                    b'A'..=b'F' => Some(c - b'A' + 10),
                    _ => None,
                };
                match (h(bytes[i + 1]), h(bytes[i + 2])) {
                    (Some(a), Some(b)) => {
                        out.push(a * 16 + b);
                        i += 3;
                    }
                    _ => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ---------------------------------------------------------------------------
// response helpers
// ---------------------------------------------------------------------------

fn ok(json: Json) -> Response {
    Response { status: 200, json }
}
fn err400(msg: &str) -> Response {
    Response { status: 400, json: Json::Obj(vec![("error".into(), Json::s(msg))]) }
}
fn err404(msg: &str) -> Response {
    Response { status: 404, json: Json::Obj(vec![("error".into(), Json::s(msg))]) }
}
fn err500(e: &StoreError) -> Response {
    Response { status: 500, json: Json::Obj(vec![("error".into(), Json::s(e.to_string()))]) }
}

fn status_text(code: u16) -> &'static str {
    match code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "OK",
    }
}

fn write_response(stream: &mut TcpStream, resp: Response) -> std::io::Result<()> {
    let body = resp.json.to_string_pretty();
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        resp.status,
        status_text(resp.status),
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|w| w == needle)
}
