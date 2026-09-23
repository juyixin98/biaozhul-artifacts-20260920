//! 本地 HTTP 验证入口。
//!
//! 仅依赖标准库手写的最小 HTTP/1.1 服务，**单线程顺序处理**（本地验证足够，
//! 且让提交协议的同步语义清晰可观察）。
//!
//! # 路由
//!
//! | 方法 路径 | 说明 |
//! |---|---|
//! | `GET  /health` | 存活检查 |
//! | `POST /format` | 格式化目录（截断重建）|
//! | `GET  /kv/<key>` | 读取单键，不存在返回 404 |
//! | `PUT  /kv/<key>` | 写入单键（请求体原样作为值；可用 `-H 'X-Value-Base64: 1'` 传二进制）|
//! | `DELETE /kv/<key>` | 删除单键 |
//! | `PUT  /kv` | 批量写，body 为 JSON：`{"items":[{"key":"...","value":"..."}]}` |
//! | `GET  /kv` | 列出全部键值（JSON 数组）|
//! | `GET  /snapshot` | 当前代次、根指针、条目数 |
//! | `GET  /admin/selftest` | 在模拟磁盘上跑全部崩溃恢复自检并返回报告 |
//!
//! 键按路径原样解释（UTF-8 字节）；特殊字符请先百分号编码。
//! 响应统一为 JSON（`GET /kv/<key>` 默认返回原始字节，加 `?format=json` 返回 JSON）。

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::time::Duration;

use crate::io_layer::RealStorage;
use crate::selftest;
use crate::store::Repository;

pub struct HttpConfig {
    pub addr: String,
    pub dir: String,
}

/// 启动服务（阻塞）。目录未格式化时自动 format。
pub fn serve(cfg: &HttpConfig) -> std::io::Result<()> {
    let dir = Path::new(&cfg.dir);
    let data_exists = dir.join(crate::format::DATA_FILE).exists();
    let super_exists = dir.join(crate::format::SUPER_FILE).exists();
    if !data_exists || !super_exists {
        RealStorage::create_files(dir)?;
        let st = RealStorage::open(dir)?;
        Repository::format(st).expect("新介质格式化不应失败");
        eprintln!("[init] 已在 {} 完成格式化", cfg.dir);
    }

    let listener = TcpListener::bind(&cfg.addr)?;
    eprintln!("[serve] 监听 http://{} ，数据目录 {}", cfg.addr, cfg.dir);
    for stream in listener.incoming() {
        match stream {
            Ok(mut stream) => {
                let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));
                if let Err(e) = handle_connection(&mut stream, dir) {
                    eprintln!("[warn] 连接处理出错: {e}");
                }
            }
            Err(e) => eprintln!("[warn] accept 失败: {e}"),
        }
    }
    Ok(())
}

fn handle_connection(stream: &mut TcpStream, dir: &Path) -> std::io::Result<()> {
    let req = match read_request(stream) {
        Ok(r) => r,
        Err(e) => {
            respond_text(stream, 400, "application/json", &j_error(&e))?;
            return Ok(());
        }
    };

    // /admin/selftest 不触碰真实仓库。
    if req.method == "GET" && req.path == "/admin/selftest" {
        let report = selftest::run();
        let body = report.render_text();
        let status = if report.all_passed() { 200 } else { 500 };
        return respond_text(stream, status, "text/plain; charset=utf-8", &body);
    }

    // POST /format 先特殊处理（重建文件后再打开仓库）。
    if req.method == "POST" && req.path == "/format" {
        RealStorage::create_files(dir)?;
        let st = RealStorage::open(dir)?;
        let repo = Repository::format(st).map_err(io_err)?;
        return respond_json(stream, 200, &j_snapshot(&repo.snapshot(), "已格式化"));
    }

    let st = RealStorage::open(dir)?;
    let mut repo = match Repository::open(st) {
        Ok(r) => r,
        Err(e) => return respond_text(stream, 500, "application/json", &j_error(&e.to_string())),
    };

    route(stream, &mut repo, &req)
}

fn io_err<E: std::fmt::Display>(e: E) -> std::io::Error {
    std::io::Error::other(e.to_string())
}

fn route(
    stream: &mut TcpStream,
    repo: &mut Repository<RealStorage>,
    req: &Request,
) -> std::io::Result<()> {
    let (path, query) = split_query(&req.path);

    match (req.method.as_str(), path.as_str()) {
        ("GET", "/health") => respond_json(stream, 200, &j_status_ok()),

        ("GET", "/snapshot") => respond_json(stream, 200, &j_snapshot(&repo.snapshot(), "")),

        ("GET", "/kv") => {
            let items: Vec<(Vec<u8>, Vec<u8>)> =
                repo.list().map(|(k, v)| (k.to_vec(), v.to_vec())).collect();
            respond_text(stream, 200, "application/json", &j_pairs(&items))
        }

        ("PUT", "/kv") => {
            // 简易 JSON 批量：{"items":[{"key": <str>, "value": <str>}]}
            match parse_batch(&req.body) {
                Ok(items) => {
                    let snap = repo
                        .put_batch(items.iter().map(|(k, v)| (k.as_bytes(), v.as_bytes())))
                        .map_err(io_err)?;
                    respond_json(stream, 200, &j_snapshot(&snap, "批量写入已提交"))
                }
                Err(msg) => respond_text(stream, 400, "application/json", &j_error(&msg)),
            }
        }

        (m, p) if p.starts_with("/kv/") => {
            let key = percent_decode(&p[4..]);
            match m {
                "GET" => match repo.get(&key) {
                    Some(value) => {
                        if query.get("format").map(String::as_str) == Some("json") {
                            let body = j_pair(&key, value);
                            respond_text(stream, 200, "application/json", &body)
                        } else {
                            respond_bytes(stream, 200, "application/octet-stream", value)
                        }
                    }
                    None => respond_text(stream, 404, "application/json", &j_error("键不存在")),
                },
                "PUT" => {
                    let value = if req
                        .headers
                        .iter()
                        .any(|(k, v)| k.eq_ignore_ascii_case("x-value-base64") && v.trim() == "1")
                    {
                        match base64_decode(std::str::from_utf8(&req.body).unwrap_or("").trim()) {
                            Some(v) => v,
                            None => {
                                return respond_text(
                                    stream,
                                    400,
                                    "application/json",
                                    &j_error("非法 base64 值"),
                                )
                            }
                        }
                    } else {
                        req.body.clone()
                    };
                    let snap = repo
                        .put_batch([(key.as_slice(), value.as_slice())])
                        .map_err(io_err)?;
                    respond_json(stream, 200, &j_snapshot(&snap, "写入已提交"))
                }
                "DELETE" => {
                    let snap = repo.delete_batch([key.as_slice()]).map_err(io_err)?;
                    respond_json(stream, 200, &j_snapshot(&snap, "删除已提交"))
                }
                _ => respond_text(stream, 405, "application/json", &j_error("方法不允许")),
            }
        }

        _ => respond_text(stream, 404, "application/json", &j_error("未知路由")),
    }
}

// ============================= HTTP 解析 =============================

struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

fn read_request(stream: &mut TcpStream) -> Result<Request, String> {
    let mut buf = Vec::with_capacity(8192);
    let mut tmp = [0u8; 4096];
    let header_end;
    loop {
        let n = stream
            .read(&mut tmp)
            .map_err(|e| format!("读取请求失败: {e}"))?;
        if n == 0 {
            return Err("连接已关闭".into());
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = find_subsequence(&buf, b"\r\n\r\n") {
            header_end = pos;
            break;
        }
        if buf.len() > 16 * 1024 * 1024 {
            return Err("请求头过大".into());
        }
    }

    let header_text = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = header_text.split("\r\n");
    let request_line = lines.next().ok_or("缺少请求行")?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().ok_or("缺少方法")?.to_string();
    let path = parts.next().ok_or("缺少路径")?.to_string();
    let _version = parts.next().ok_or("缺少 HTTP 版本")?;

    let mut headers = Vec::new();
    let mut content_length = 0usize;
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            let (k, v) = (k.trim().to_string(), v.trim().to_string());
            if k.eq_ignore_ascii_case("content-length") {
                content_length = v.parse().map_err(|_| "非法 Content-Length")?;
            }
            headers.push((k, v));
        }
    }

    let body_start = header_end + 4;
    let mut body = buf[body_start..].to_vec();
    while body.len() < content_length {
        let n = stream
            .read(&mut tmp)
            .map_err(|e| format!("读取 body 失败: {e}"))?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    body.truncate(content_length);

    Ok(Request {
        method,
        path,
        headers,
        body,
    })
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

// ============================= 响应 =============================

fn respond_text(
    stream: &mut TcpStream,
    status: u16,
    content_type: &str,
    body: &str,
) -> std::io::Result<()> {
    respond_bytes(stream, status, content_type, body.as_bytes())
}

fn respond_bytes(
    stream: &mut TcpStream,
    status: u16,
    content_type: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        405 => "Method Not Allowed",
        500 => "Internal Server Error",
        _ => "OK",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

fn respond_json(stream: &mut TcpStream, status: u16, body: &str) -> std::io::Result<()> {
    respond_text(stream, status, "application/json", body)
}

// ============================= JSON / 编码辅助 =============================
//
// 不用第三方 serde：请求只需要解析固定形状，响应只需要几种值。这里提供一个
// 最小的 JSON 值枚举用于**写出**，类型区分字符串/数字/布尔/对象，避免用
// 字符串启发式拼接导致的引号/转义错误。

enum J {
    Str(String),
    /// 原始 JSON 字面量（数字、true/false、已序列化的嵌套值）。
    Raw(String),
    Obj(Vec<(&'static str, J)>),
}

impl J {
    fn s(v: impl Into<String>) -> J {
        J::Str(v.into())
    }
    fn bytes(v: &[u8]) -> J {
        J::Str(String::from_utf8_lossy(v).into_owned())
    }
    fn n(v: impl std::fmt::Display) -> J {
        J::Raw(v.to_string())
    }
    fn b(v: bool) -> J {
        J::Raw(v.to_string())
    }
    fn write(&self, out: &mut String) {
        match self {
            J::Str(s) => write_json_string(s, out),
            J::Raw(r) => out.push_str(r),
            J::Obj(fields) => {
                out.push('{');
                for (i, (k, v)) in fields.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_json_string(k, out);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

/// 把一个（可能含非 ASCII 的）字符串写为合法 JSON 字符串。
fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            // 控制字符必须转义；其余（含中文）按 UTF-8 原样输出，合法且可读。
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn j_error(msg: &str) -> String {
    J::Obj(vec![("ok", J::b(false)), ("error", J::s(msg))]).render()
}

fn j_status_ok() -> String {
    J::Obj(vec![("status", J::s("ok"))]).render()
}

fn j_pair(key: &[u8], value: &[u8]) -> String {
    J::Obj(vec![("key", J::bytes(key)), ("value", J::bytes(value))]).render()
}

/// 对象数组（GET /kv 用）。
fn j_pairs(items: &[(Vec<u8>, Vec<u8>)]) -> String {
    let mut out = String::from("[");
    for (i, (k, v)) in items.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&j_pair(k, v));
    }
    out.push(']');
    out
}

fn j_snapshot(snap: &crate::store::Snapshot, note: &str) -> String {
    let root = J::Obj(vec![
        ("offset", J::n(snap.root.offset)),
        ("len", J::n(snap.root.len)),
        ("crc", J::s(format!("0x{:08x}", snap.root.crc))),
        ("empty", J::b(snap.root.is_empty())),
    ]);
    J::Obj(vec![
        ("ok", J::b(true)),
        ("generation", J::n(snap.generation)),
        ("entries", J::n(snap.entries)),
        ("root", root),
        ("note", J::s(note)),
    ])
    .render()
}

impl J {
    fn render(self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }
}

fn split_query(path: &str) -> (String, std::collections::HashMap<String, String>) {
    let mut q = std::collections::HashMap::new();
    if let Some((p, query)) = path.split_once('?') {
        for pair in query.split('&') {
            if let Some((k, v)) = pair.split_once('=') {
                q.insert(
                    String::from_utf8_lossy(&percent_decode(k)).into_owned(),
                    String::from_utf8_lossy(&percent_decode(v)).into_owned(),
                );
            }
        }
        (p.to_string(), q)
    } else {
        (path.to_string(), q)
    }
}

/// 百分号解码（%XX 与 +）。非法转义原样保留。
fn percent_decode(s: &str) -> Vec<u8> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let h = std::str::from_utf8(&bytes[i + 1..i + 3])
                    .ok()
                    .and_then(|x| u8::from_str_radix(x, 16).ok());
                match h {
                    Some(v) => {
                        out.push(v);
                        i += 3;
                    }
                    None => {
                        out.push(b'%');
                        i += 1;
                    }
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
    out
}

/// 极简标准字母表 base64 解码（HTTP 头传二进制值用）。
fn base64_decode(s: &str) -> Option<Vec<u8>> {
    const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut val = [255u8; 256];
    for (i, &c) in T.iter().enumerate() {
        val[c as usize] = i as u8;
    }
    let s = s.trim_end_matches('=');
    let mut out = Vec::new();
    let mut acc: u32 = 0;
    let mut bits = 0u32;
    for c in s.bytes() {
        if c == b'\n' || c == b'\r' {
            continue;
        }
        let v = *val.get(c as usize)?;
        if v == 255 {
            return None;
        }
        acc = (acc << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// 解析 `{"items":[{"key":"...","value":"..."}]}`（键值均为 JSON 字符串）。
/// 不做通用 JSON 解析器，只识别本服务自己文档化的固定形状。
fn parse_batch(body: &[u8]) -> Result<Vec<(String, String)>, String> {
    let s = std::str::from_utf8(body).map_err(|_| "body 不是 UTF-8 JSON")?;
    let s = s.trim();
    let arr_open = s.find('[').ok_or("缺少 items 数组")?;
    let arr_close = s.rfind(']').ok_or("items 数组未闭合")?;
    let inner = &s[arr_open + 1..arr_close];
    let mut items = Vec::new();
    for obj in split_top_objects(inner) {
        let key = extract_json_string_field(obj, "key").ok_or("缺少字符串字段 key")?;
        let value = extract_json_string_field(obj, "value").ok_or("缺少字符串字段 value")?;
        items.push((key, value));
    }
    if items.is_empty() {
        return Err("items 为空（至少需要一项）".into());
    }
    Ok(items)
}

fn split_top_objects(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut start = None;
    let mut in_str = false;
    let mut esc = false;
    for (i, c) in s.char_indices() {
        if in_str {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        match c {
            '"' => in_str = true,
            '{' => {
                if depth == 0 {
                    start = Some(i);
                }
                depth += 1;
            }
            '}' => {
                depth -= 1;
                if depth == 0 {
                    if let Some(st) = start.take() {
                        out.push(&s[st..=i]);
                    }
                }
            }
            _ => {}
        }
    }
    out
}

fn extract_json_string_field(obj: &str, field: &str) -> Option<String> {
    let pat = format!("\"{field}\"");
    let p = obj.find(&pat)? + pat.len();
    let rest = obj[p..].trim_start();
    let rest = rest.strip_prefix(':')?.trim_start();
    parse_json_string(rest)
}

fn parse_json_string(s: &str) -> Option<String> {
    let s = s.strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = s.char_indices();
    while let Some((_, c)) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => {
                let (_, e) = chars.next()?;
                match e {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'n' => out.push('\n'),
                    't' => out.push('\t'),
                    'r' => out.push('\r'),
                    'u' => {
                        let hex: String = (0..4)
                            .filter_map(|_| chars.next().map(|(_, c)| c))
                            .collect();
                        let code = u32::from_str_radix(&hex, 16).ok()?;
                        if let Some(ch) = char::from_u32(code) {
                            out.push(ch);
                        }
                    }
                    _ => return None,
                }
            }
            other => out.push(other),
        }
    }
    None
}
