//! # 本地 TCP 测试服务
//!
//! 纯 `std::net` 实现，单线程 accept、每连接一个线程；只监听
//! `127.0.0.1`，不需要任何特权。协议为**换行分隔的 JSON**（NDJSON）：
//! 每个请求一行，每行返回一个 JSON 响应。JSON 解析与序列化均为
//! 本模块手写（见 [`json`]），不使用三方库。
//!
//! ## 请求
//!
//! 统一字段：`op`、`now_ms`（可选；缺省用系统时钟）。
//!
//! ### `fragment` —— 送一片（逻辑字段）
//!
//! ```json
//! {"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,
//!  "id":4660,"offset":0,"mf":true,"payload_hex":"aa...","now_ms":0}
//! ```
//!
//! ### `raw_packet` —— 送一个完整 IPv4 报文（hex，走手写 IPv4 解析器）
//!
//! ```json
//! {"op":"raw_packet","packet_hex":"4500...","now_ms":10}
//! ```
//!
//! ### 其它操作
//!
//! * `{"op":"purge","now_ms":5000}`：显式清理过期组；
//! * `{"op":"status"}`：返回引擎统计；
//! * `{"op":"reset"}`：清空引擎。
//!
//! 响应统一含 `"status":"ok"` 或 `"status":"error"`，重组完成时
//! 额外给出 `result:"completed"`、`payload_hex` 与总长度。

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::reassembly::{self, AddResult, Config, Reassembler};

/// 服务运行参数。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ServerConfig {
    pub reassembly: Config,
}

/// 在 `127.0.0.1:port` 上启动服务并阻塞处理连接。
///
/// `port` 传 `0` 表示由操作系统分配端口；实际端口可通过
/// 返回的监听器（测试用）获知——见 [`bind`] + [`serve_listener`]。
pub fn serve(port: u16, config: ServerConfig) -> std::io::Result<()> {
    let listener = bind(port)?;
    log_listening(&listener);
    serve_listener(listener, config)
}

/// 仅绑定，便于测试拿到实际端口。
pub fn bind(port: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", port))
}

fn log_listening(listener: &TcpListener) {
    if let Ok(addr) = listener.local_addr() {
        eprintln!("[reasm-server] listening on http://{addr} (raw TCP, NDJSON)");
    }
}

/// 在已绑定的监听器上服务（阻塞）。
pub fn serve_listener(listener: TcpListener, config: ServerConfig) -> std::io::Result<()> {
    let engine = Arc::new(Mutex::new(Reassembler::new(config.reassembly)));
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let peer = stream.peer_addr().ok();
                if let Err(e) = stream.set_nodelay(true) {
                    eprintln!("[reasm-server] set_nodelay failed: {e}");
                }
                let engine = Arc::clone(&engine);
                std::thread::spawn(move || {
                    if let Err(e) = handle_connection(stream, &engine) {
                        eprintln!("[reasm-server] connection {peer:?} ended: {e}");
                    }
                });
            }
            Err(e) => eprintln!("[reasm-server] accept failed: {e}"),
        }
    }
    Ok(())
}

/// 当前单调毫秒（自 UNIX_EPOCH）。
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn handle_connection(stream: TcpStream, engine: &Arc<Mutex<Reassembler>>) -> std::io::Result<()> {
    let peer = stream.peer_addr().ok();
    let writer = stream.try_clone()?;
    let mut input = BufReader::new(stream);
    let mut output = BufWriter::new(writer);
    let mut line = String::new();

    loop {
        line.clear();
        let n = input.read_line(&mut line)?;
        if n == 0 {
            let _ = output.flush();
            return Ok(()); // 对端关闭
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let response = match json::parse(trimmed) {
            Ok(req) => dispatch(req, engine.as_ref()),
            Err(e) => error_response("invalid_json", &e.to_string()),
        };
        serde_json_write(&response, &mut output)?;
        output.write_all(b"\n")?;
        output.flush()?;
        let _ = peer;
    }
}

fn dispatch(request: json::Json, engine: &std::sync::Mutex<Reassembler>) -> json::Json {
    let obj = match &request {
        json::Json::Object(m) => m,
        _ => return error_response("invalid_request", "request must be a JSON object"),
    };
    let op = match obj.get("op").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => return error_response("invalid_request", "missing string field \"op\""),
    };
    let now = match obj.get("now_ms").and_then(|v| v.as_u64()) {
        Some(n) => n,
        None => now_ms(),
    };

    match op {
        "fragment" => handle_fragment(obj, engine, now),
        "raw_packet" => handle_raw_packet(obj, engine, now),
        "purge" => handle_purge(obj, engine, now),
        "status" => handle_status(engine),
        "reset" => handle_reset(engine),
        other => error_response(
            "invalid_request",
            &format!("unknown op {other:?}; expected fragment|raw_packet|purge|status|reset"),
        ),
    }
}

fn handle_fragment(
    obj: &BTreeMap<String, json::Json>,
    engine: &std::sync::Mutex<Reassembler>,
    now: u64,
) -> json::Json {
    let parsed = match parse_fragment_fields(obj) {
        Ok(v) => v,
        Err(e) => return e,
    };
    let mut e = engine.lock().expect("engine mutex poisoned");
    let result = e.add_fragment(&parsed.header, &parsed.payload, now);
    result_to_json(result, e.buffered_bytes())
}

struct FragmentInput {
    header: crate::ipv4::Ipv4Header,
    payload: Vec<u8>,
}

#[allow(clippy::too_many_lines)]
fn parse_fragment_fields(obj: &BTreeMap<String, json::Json>) -> Result<FragmentInput, json::Json> {
    use crate::ipv4::Ipv4Header;

    let src = require_ipv4(obj, "src")?;
    let dst = require_ipv4(obj, "dst")?;
    let protocol = require_u64(obj, "protocol")? as u8;
    let identification = require_u64(obj, "id")? as u16;
    let offset = require_u64(obj, "offset")? as usize;
    if !offset.is_multiple_of(8) {
        return Err(error_response(
            "invalid_fragment",
            &format!("offset must be a multiple of 8, got {offset}"),
        ));
    }
    let mf = obj.get("mf").and_then(|v| v.as_bool()).unwrap_or(false);
    let df = obj.get("df").and_then(|v| v.as_bool()).unwrap_or(false);
    let ttl = obj
        .get("ttl")
        .and_then(|v| v.as_u64())
        .map(|v| v as u8)
        .unwrap_or(64);

    let payload_hex = obj
        .get("payload_hex")
        .and_then(|v| v.as_str())
        .ok_or_else(|| error_response("invalid_request", "missing string field \"payload_hex\""))?;
    let payload = match hex_decode(payload_hex) {
        Ok(p) => p,
        Err(msg) => return Err(error_response("invalid_hex", &msg)),
    };

    Ok(FragmentInput {
        header: Ipv4Header {
            ihl: 5,
            header_len: 20,
            dscp_ecn: 0,
            total_length: (20 + payload.len()) as u16,
            identification,
            reserved_flag: false,
            df,
            mf,
            fragment_offset: offset,
            ttl,
            protocol,
            checksum: 0,
            src,
            dst,
            options: Vec::new(),
        },
        payload,
    })
}

fn handle_raw_packet(
    obj: &BTreeMap<String, json::Json>,
    engine: &std::sync::Mutex<Reassembler>,
    now: u64,
) -> json::Json {
    let hex = match obj.get("packet_hex").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => return error_response("invalid_request", "missing string field \"packet_hex\""),
    };
    let packet = match hex_decode(hex) {
        Ok(p) => p,
        Err(msg) => return error_response("invalid_hex", &msg),
    };
    let mut e = engine.lock().expect("engine mutex poisoned");
    result_to_json(e.add_packet(&packet, now), e.buffered_bytes())
}

fn handle_purge(
    obj: &BTreeMap<String, json::Json>,
    engine: &std::sync::Mutex<Reassembler>,
    now: u64,
) -> json::Json {
    let mut e = engine.lock().expect("engine mutex poisoned");
    let purged = e.purge_expired(now);
    let mut m = ok_base();
    let _ = obj;
    m.insert("op".into(), json::Json::String("purge".into()));
    m.insert(
        "purged".into(),
        json::Json::Array(
            purged
                .iter()
                .map(|(k, bytes)| {
                    let mut entry = BTreeMap::new();
                    entry.insert("key".into(), json::Json::String(k.to_string()));
                    entry.insert("freed_bytes".into(), json::Json::Uint(*bytes as u64));
                    json::Json::Object(entry)
                })
                .collect(),
        ),
    );
    insert_stats(&mut m, &e);
    json::Json::Object(m)
}

fn handle_status(engine: &std::sync::Mutex<Reassembler>) -> json::Json {
    let e = engine.lock().expect("engine mutex poisoned");
    let mut m = ok_base();
    m.insert("op".into(), json::Json::String("status".into()));
    insert_stats(&mut m, &e);
    json::Json::Object(m)
}

fn handle_reset(engine: &std::sync::Mutex<Reassembler>) -> json::Json {
    let mut e = engine.lock().expect("engine mutex poisoned");
    e.reset();
    let mut m = ok_base();
    m.insert("op".into(), json::Json::String("reset".into()));
    insert_stats(&mut m, &e);
    json::Json::Object(m)
}

fn result_to_json(
    result: Result<AddResult, reassembly::ReassemblyError>,
    buffered: usize,
) -> json::Json {
    match result {
        Ok(AddResult::Pending(s)) => {
            let mut m = ok_base();
            m.insert("result".into(), json::Json::String("pending".into()));
            m.insert("key".into(), json::Json::String(s.key.to_string()));
            m.insert(
                "fragment_count".into(),
                json::Json::Uint(s.fragment_count as u64),
            );
            m.insert(
                "buffered_bytes".into(),
                json::Json::Uint(s.buffered_bytes as u64),
            );
            m.insert(
                "contiguous_from_zero".into(),
                json::Json::Uint(s.contiguous_from_zero as u64),
            );
            m.insert(
                "total_length".into(),
                match s.total_length {
                    Some(n) => json::Json::Uint(n as u64),
                    None => json::Json::Null,
                },
            );
            m.insert(
                "engine_buffered_bytes".into(),
                json::Json::Uint(buffered as u64),
            );
            json::Json::Object(m)
        }
        Ok(AddResult::Completed(d)) => {
            let mut m = ok_base();
            m.insert("result".into(), json::Json::String("completed".into()));
            m.insert("key".into(), json::Json::String(d.key.to_string()));
            m.insert(
                "fragment_count".into(),
                json::Json::Uint(d.fragment_count as u64),
            );
            m.insert(
                "total_length".into(),
                json::Json::Uint(d.payload.len() as u64),
            );
            m.insert(
                "completed_at_ms".into(),
                json::Json::Uint(d.completed_at_ms),
            );
            m.insert(
                "payload_hex".into(),
                json::Json::String(hex_encode(&d.payload)),
            );
            m.insert(
                "engine_buffered_bytes".into(),
                json::Json::Uint(buffered as u64),
            );
            json::Json::Object(m)
        }
        Err(err) => {
            let (code, message) = error_code_and_message(&err);
            error_response(code, &message)
        }
    }
}

fn error_code_and_message(err: &reassembly::ReassemblyError) -> (&'static str, String) {
    use reassembly::ReassemblyError::*;
    match err {
        ParseFailed(_) => ("parse_failed", err.to_string()),
        InvalidFragment(_) => ("invalid_fragment", err.to_string()),
        OverlapConflict { .. } => ("overlap_conflict", err.to_string()),
        OversizedDatagram { .. } => ("oversized_datagram", err.to_string()),
        BudgetExceeded { .. } => ("budget_exceeded", err.to_string()),
    }
}

fn insert_stats(m: &mut BTreeMap<String, json::Json>, e: &Reassembler) {
    m.insert(
        "pending_datagrams".into(),
        json::Json::Uint(e.pending_count() as u64),
    );
    m.insert(
        "buffered_bytes".into(),
        json::Json::Uint(e.buffered_bytes() as u64),
    );
    let cfg = e.config();
    m.insert(
        "fragment_ttl_ms".into(),
        json::Json::Uint(cfg.fragment_ttl_ms),
    );
    m.insert(
        "total_memory_budget".into(),
        json::Json::Uint(cfg.total_memory_budget as u64),
    );
    m.insert(
        "max_datagram_payload".into(),
        json::Json::Uint(cfg.max_datagram_payload as u64),
    );
    m.insert(
        "overlap_policy".into(),
        json::Json::String(format!("{:?}", e.policy())),
    );
}

fn require_u64(obj: &BTreeMap<String, json::Json>, field: &str) -> Result<u64, json::Json> {
    match obj.get(field).and_then(|v| v.as_u64()) {
        Some(n) => Ok(n),
        None => Err(error_response(
            "invalid_request",
            &format!("missing or non-integer field {field:?}"),
        )),
    }
}

fn require_ipv4(
    obj: &BTreeMap<String, json::Json>,
    field: &str,
) -> Result<std::net::Ipv4Addr, json::Json> {
    let s = obj.get(field).and_then(|v| v.as_str()).ok_or_else(|| {
        error_response(
            "invalid_request",
            &format!("missing string field {field:?}"),
        )
    })?;
    s.parse::<std::net::Ipv4Addr>().map_err(|_| {
        error_response(
            "invalid_ip",
            &format!("field {field:?}={s:?} is not an IPv4 address"),
        )
    })
}

fn ok_base() -> BTreeMap<String, json::Json> {
    let mut m = BTreeMap::new();
    m.insert("status".into(), json::Json::String("ok".into()));
    m
}

fn error_response(code: &str, message: &str) -> json::Json {
    let mut m = BTreeMap::new();
    m.insert("status".into(), json::Json::String("error".into()));
    m.insert("error".into(), json::Json::String(code.into()));
    m.insert("message".into(), json::Json::String(message.into()));
    json::Json::Object(m)
}

// ---------------- hex ----------------

/// 十六进制解码：允许空白与冒号分隔（如 `aa:bb`）。
pub fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    let cleaned: Vec<char> = s
        .chars()
        .filter(|c| !c.is_whitespace() && *c != ':')
        .collect();
    if !cleaned.len().is_multiple_of(2) {
        return Err(format!("odd number of hex digits: {}", cleaned.len()));
    }
    let mut out = Vec::with_capacity(cleaned.len() / 2);
    let digits: String = cleaned.into_iter().collect();
    let (pairs, _no_remainder) = digits.as_bytes().as_chunks::<2>();
    for pair in pairs {
        let hi = hex_value(pair[0])?;
        let lo = hex_value(pair[1])?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

fn hex_value(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(format!("invalid hex digit: {:?}", c as char)),
    }
}

/// 小写、无分隔的十六进制编码。
pub fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

fn serde_json_write(value: &json::Json, w: &mut impl Write) -> std::io::Result<()> {
    w.write_all(json::to_string(value).as_bytes())
}

// ============================================================
// 手写 JSON：解析器 + 序列化（无三方依赖）
// ============================================================

/// 极简 JSON 值模型（对象用 BTreeMap 以保证输出确定）。
pub mod json {
    use std::collections::BTreeMap;

    #[derive(Debug, Clone, PartialEq)]
    pub enum Json {
        Null,
        Bool(bool),
        /// 非负整数（协议里没有浮点需求）
        Uint(u64),
        /// 兼容负数（解析器接受，接口基本不用）
        Int(i64),
        String(String),
        Array(Vec<Json>),
        Object(BTreeMap<String, Json>),
    }

    impl Json {
        pub fn as_str(&self) -> Option<&str> {
            match self {
                Json::String(s) => Some(s),
                _ => None,
            }
        }
        pub fn as_bool(&self) -> Option<bool> {
            match self {
                Json::Bool(b) => Some(*b),
                _ => None,
            }
        }
        pub fn as_u64(&self) -> Option<u64> {
            match self {
                Json::Uint(n) => Some(*n),
                Json::Int(n) if *n >= 0 => Some(*n as u64),
                _ => None,
            }
        }
    }

    /// JSON 解析错误。
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct JsonError {
        pub message: String,
        pub position: usize,
    }

    impl std::fmt::Display for JsonError {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(
                f,
                "JSON parse error at byte {}: {}",
                self.position, self.message
            )
        }
    }

    impl std::error::Error for JsonError {}

    /// 解析一个 JSON 文档（前后允许空白；不允许文档后有多余值）。
    pub fn parse(input: &str) -> Result<Json, JsonError> {
        let bytes = input.as_bytes();
        let mut p = Parser { bytes, pos: 0 };
        p.skip_ws();
        let value = p.parse_value()?;
        p.skip_ws();
        if p.pos != bytes.len() {
            return Err(p.err("trailing characters after JSON document"));
        }
        Ok(value)
    }

    struct Parser<'a> {
        bytes: &'a [u8],
        pos: usize,
    }

    impl<'a> Parser<'a> {
        fn err(&self, msg: &str) -> JsonError {
            JsonError {
                message: msg.to_string(),
                position: self.pos,
            }
        }

        fn peek(&self) -> Option<u8> {
            self.bytes.get(self.pos).copied()
        }

        fn bump(&mut self) -> Option<u8> {
            let c = self.peek()?;
            self.pos += 1;
            Some(c)
        }

        fn skip_ws(&mut self) {
            while let Some(c) = self.peek() {
                if matches!(c, b' ' | b'\t' | b'\r' | b'\n') {
                    self.pos += 1;
                } else {
                    break;
                }
            }
        }

        fn expect(&mut self, expected: u8) -> Result<(), JsonError> {
            match self.bump() {
                Some(c) if c == expected => Ok(()),
                Some(c) => Err(self.err(&format!(
                    "expected {:?}, found {:?}",
                    expected as char, c as char
                ))),
                None => Err(self.err("unexpected end of input")),
            }
        }

        fn parse_value(&mut self) -> Result<Json, JsonError> {
            self.skip_ws();
            match self.peek() {
                Some(b'{') => self.parse_object(),
                Some(b'[') => self.parse_array(),
                Some(b'"') => self.parse_string().map(Json::String),
                Some(b't') | Some(b'f') => self.parse_bool(),
                Some(b'n') => self.parse_null(),
                Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
                Some(c) => Err(self.err(&format!("unexpected character {:?}", c as char))),
                None => Err(self.err("unexpected end of input")),
            }
        }

        fn parse_object(&mut self) -> Result<Json, JsonError> {
            self.expect(b'{')?;
            let mut map = BTreeMap::new();
            self.skip_ws();
            if self.peek() == Some(b'}') {
                self.pos += 1;
                return Ok(Json::Object(map));
            }
            loop {
                self.skip_ws();
                let key = self.parse_string()?;
                self.skip_ws();
                self.expect(b':')?;
                let value = self.parse_value()?;
                map.insert(key, value);
                self.skip_ws();
                match self.bump() {
                    Some(b',') => continue,
                    Some(b'}') => break,
                    Some(c) => return Err(self.err(&format!("expected ',' or '}}', got {c:?}"))),
                    None => return Err(self.err("unterminated object")),
                }
            }
            Ok(Json::Object(map))
        }

        fn parse_array(&mut self) -> Result<Json, JsonError> {
            self.expect(b'[')?;
            let mut items = Vec::new();
            self.skip_ws();
            if self.peek() == Some(b']') {
                self.pos += 1;
                return Ok(Json::Array(items));
            }
            loop {
                let value = self.parse_value()?;
                items.push(value);
                self.skip_ws();
                match self.bump() {
                    Some(b',') => continue,
                    Some(b']') => break,
                    Some(c) => return Err(self.err(&format!("expected ',' or ']', got {c:?}"))),
                    None => return Err(self.err("unterminated array")),
                }
            }
            Ok(Json::Array(items))
        }

        fn parse_string(&mut self) -> Result<String, JsonError> {
            self.expect(b'"')?;
            let mut out = String::new();
            loop {
                match self.bump() {
                    Some(b'"') => break,
                    Some(b'\\') => match self.bump() {
                        Some(b'"') => out.push('"'),
                        Some(b'\\') => out.push('\\'),
                        Some(b'/') => out.push('/'),
                        Some(b'b') => out.push('\u{0008}'),
                        Some(b'f') => out.push('\u{000C}'),
                        Some(b'n') => out.push('\n'),
                        Some(b'r') => out.push('\r'),
                        Some(b't') => out.push('\t'),
                        Some(b'u') => {
                            let cp = self.parse_hex4()?;
                            // 处理 UTF-16 代理对
                            let cp = if (0xD800..=0xDBFF).contains(&cp) {
                                if self.bump() != Some(b'\\') || self.bump() != Some(b'u') {
                                    return Err(
                                        self.err("expected low surrogate after high surrogate")
                                    );
                                }
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(self.err("invalid low surrogate"));
                                }
                                0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                            } else {
                                cp
                            };
                            match char::from_u32(cp) {
                                Some(c) => out.push(c),
                                None => return Err(self.err("invalid unicode scalar")),
                            }
                        }
                        Some(c) => return Err(self.err(&format!("bad escape \\{c}"))),
                        None => return Err(self.err("unterminated escape")),
                    },
                    Some(c) if c < 0x80 => out.push(c as char),
                    Some(_) => {
                        // UTF-8 多字节：原样收集
                        let start = self.pos - 1;
                        let len = utf8_len(self.bytes[start]);
                        if self.bytes.len() < start + len {
                            return Err(self.err("truncated UTF-8 in string"));
                        }
                        match std::str::from_utf8(&self.bytes[start..start + len]) {
                            Ok(s) => out.push_str(s),
                            Err(_) => return Err(self.err("invalid UTF-8 in string")),
                        }
                        self.pos = start + len;
                    }
                    None => return Err(self.err("unterminated string")),
                }
            }
            Ok(out)
        }

        fn parse_hex4(&mut self) -> Result<u32, JsonError> {
            let mut value = 0u32;
            for _ in 0..4 {
                let c = self
                    .bump()
                    .ok_or_else(|| self.err("incomplete \\uXXXX escape"))?;
                let d = match c {
                    b'0'..=b'9' => (c - b'0') as u32,
                    b'a'..=b'f' => (c - b'a' + 10) as u32,
                    b'A'..=b'F' => (c - b'A' + 10) as u32,
                    _ => return Err(self.err("invalid hex digit in \\u escape")),
                };
                value = (value << 4) | d;
            }
            Ok(value)
        }

        fn parse_bool(&mut self) -> Result<Json, JsonError> {
            if self.bytes[self.pos..].starts_with(b"true") {
                self.pos += 4;
                Ok(Json::Bool(true))
            } else if self.bytes[self.pos..].starts_with(b"false") {
                self.pos += 5;
                Ok(Json::Bool(false))
            } else {
                Err(self.err("invalid literal"))
            }
        }

        fn parse_null(&mut self) -> Result<Json, JsonError> {
            if self.bytes[self.pos..].starts_with(b"null") {
                self.pos += 4;
                Ok(Json::Null)
            } else {
                Err(self.err("invalid literal"))
            }
        }

        fn parse_number(&mut self) -> Result<Json, JsonError> {
            let start = self.pos;
            let mut negative = false;
            if self.peek() == Some(b'-') {
                negative = true;
                self.pos += 1;
            }
            let digits_start = self.pos;
            while let Some(c) = self.peek() {
                if c.is_ascii_digit() {
                    self.pos += 1;
                } else {
                    break;
                }
            }
            if self.pos == digits_start {
                return Err(self.err("expected digit"));
            }
            // 本协议不支持浮点/指数：遇到即报错（显式错误类型）
            if matches!(self.peek(), Some(b'.' | b'e' | b'E')) {
                return Err(self.err("only integers are supported in this protocol"));
            }
            let text = std::str::from_utf8(&self.bytes[start..self.pos])
                .map_err(|_| self.err("bad number"))?;
            if negative {
                text.parse::<i64>()
                    .map(Json::Int)
                    .map_err(|_| self.err("integer out of i64 range"))
            } else {
                text.parse::<u64>()
                    .map(Json::Uint)
                    .map_err(|_| self.err("integer out of u64 range"))
            }
        }
    }

    fn utf8_len(first: u8) -> usize {
        if first < 0x80 {
            1
        } else if first >> 5 == 0b110 {
            2
        } else if first >> 4 == 0b1110 {
            3
        } else {
            4
        }
    }

    /// 紧凑 JSON 序列化（无空白，键经 BTreeMap 排序，输出确定）。
    pub fn to_string(value: &Json) -> String {
        let mut out = String::new();
        write_value(value, &mut out);
        out
    }

    fn write_value(value: &Json, out: &mut String) {
        match value {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Uint(n) => out.push_str(&n.to_string()),
            Json::Int(n) => out.push_str(&n.to_string()),
            Json::String(s) => write_json_string(s, out),
            Json::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_value(item, out);
                }
                out.push(']');
            }
            Json::Object(map) => {
                out.push('{');
                for (i, (k, v)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_json_string(k, out);
                    out.push(':');
                    write_value(v, out);
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
                '\u{0008}' => out.push_str("\\b"),
                '\u{000C}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
    }
}

// 防止未使用告警（Read trait 在某些平台扩展时使用）
#[allow(dead_code)]
fn assert_read_trait(mut r: impl Read) -> std::io::Result<Vec<u8>> {
    let mut v = Vec::new();
    r.read_to_end(&mut v)?;
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::json::{self, Json};
    use super::*;
    use crate::ipv4;
    use std::collections::BTreeMap;
    use std::net::TcpStream;
    use std::time::Duration;

    #[test]
    fn hex_roundtrip() {
        let bytes = vec![0x00, 0xff, 0x10, 0xab];
        assert_eq!(hex_decode("00ff10ab").unwrap(), bytes);
        assert_eq!(hex_decode("00 ff:10 AB").unwrap(), bytes);
        assert!(hex_decode("abc").is_err());
        assert!(hex_decode("zz").is_err());
        assert_eq!(hex_encode(&bytes), "00ff10ab");
    }

    #[test]
    fn json_parser_supports_needed_types() {
        assert_eq!(json::parse("null").unwrap(), Json::Null);
        assert_eq!(json::parse("true").unwrap(), Json::Bool(true));
        assert_eq!(json::parse(" 42 ").unwrap(), Json::Uint(42));
        assert_eq!(json::parse("-7").unwrap(), Json::Int(-7));
        assert_eq!(
            json::parse("\"a\\nb\"").unwrap(),
            Json::String("a\nb".into())
        );
        let parsed = json::parse(r#"{"op":"fragment","mf":true,"n":3}"#).unwrap();
        match parsed {
            Json::Object(m) => {
                assert_eq!(m.get("op").unwrap().as_str(), Some("fragment"));
                assert_eq!(m.get("mf").unwrap().as_bool(), Some(true));
                assert_eq!(m.get("n").unwrap().as_u64(), Some(3));
            }
            _ => panic!(),
        }
    }

    #[test]
    fn json_parser_rejects_bad_input_with_positions() {
        let err = json::parse("{").unwrap_err();
        assert!(err.position > 0);
        assert!(json::parse("tru").is_err());
        assert!(json::parse("[1,]").is_err());
        assert!(json::parse("1.5").is_err(), "floats explicitly rejected");
        assert!(json::parse("1 2").is_err());
    }

    #[test]
    fn json_serializer_is_deterministic() {
        let mut m = BTreeMap::new();
        m.insert("b".into(), Json::Uint(2));
        m.insert("a".into(), Json::Uint(1));
        let v = Json::Object(m);
        assert_eq!(json::to_string(&v), r#"{"a":1,"b":2}"#);
    }

    // ---------- 端到端：真实 TCP 连接 ----------

    struct TestServer {
        port: u16,
        _join: Option<std::thread::JoinHandle<()>>,
    }

    impl TestServer {
        fn start(ttl_ms: u64, budget: usize) -> Self {
            let listener = bind(0).unwrap();
            let port = listener.local_addr().unwrap().port();
            let cfg = ServerConfig {
                reassembly: Config {
                    fragment_ttl_ms: ttl_ms,
                    total_memory_budget: budget,
                    max_datagram_payload: 65_535,
                },
            };
            let join = std::thread::spawn(move || {
                serve_listener(listener, cfg).unwrap();
            });
            TestServer {
                port,
                _join: Some(join),
            }
        }

        fn exchange(&self, line: &str) -> String {
            let mut stream = TcpStream::connect(("127.0.0.1", self.port)).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            stream.write_all(line.as_bytes()).unwrap();
            stream.write_all(b"\n").unwrap();
            stream.flush().unwrap();
            let mut reader = BufReader::new(stream);
            let mut response = String::new();
            reader.read_line(&mut response).unwrap();
            response
        }
    }

    fn obj_get<'a>(v: &'a Json, key: &str) -> &'a Json {
        match v {
            Json::Object(m) => m.get(key).unwrap(),
            _ => panic!("not an object: {v:?}"),
        }
    }

    #[test]
    fn tcp_end_to_end_fragment_reassembly() {
        let server = TestServer::start(1_000, 1 << 20);

        // 首片（偏移 0，MF）
        let head_req = format!(
            r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":0,"mf":true,"payload_hex":"{}","now_ms":0}}"#,
            hex_encode(&[0xAA; 16])
        );
        let resp = json::parse(&server.exchange(&head_req)).unwrap();
        assert_eq!(obj_get(&resp, "status").as_str(), Some("ok"));
        assert_eq!(obj_get(&resp, "result").as_str(), Some("pending"));

        // 尾片
        let tail_req = format!(
            r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":16,"mf":false,"payload_hex":"{}","now_ms":10}}"#,
            hex_encode(&[0xBB; 16])
        );
        let resp = json::parse(&server.exchange(&tail_req)).unwrap();
        assert_eq!(obj_get(&resp, "result").as_str(), Some("completed"));
        assert_eq!(obj_get(&resp, "total_length").as_u64(), Some(32));
        let mut expected = vec![0xAA; 16];
        expected.extend(std::iter::repeat_n(0xBB, 16));
        assert_eq!(
            obj_get(&resp, "payload_hex").as_str().unwrap(),
            hex_encode(&expected)
        );
    }

    #[test]
    fn tcp_end_to_end_raw_packet_and_overlap_error() {
        let server = TestServer::start(1_000, 1 << 20);
        let pkt = ipv4::build_fragment(
            (1, 1, 1, 1),
            (2, 2, 2, 2),
            6,
            55,
            0,
            false,
            (0..10u8).collect(),
        );
        let req = format!(
            r#"{{"op":"raw_packet","packet_hex":"{}"}}"#,
            hex_encode(&pkt)
        );
        let resp = json::parse(&server.exchange(&req)).unwrap();
        assert_eq!(obj_get(&resp, "result").as_str(), Some("completed"));

        // 坏 JSON
        let resp = json::parse(&server.exchange("{not json")).unwrap();
        assert_eq!(obj_get(&resp, "status").as_str(), Some("error"));
        assert_eq!(obj_get(&resp, "error").as_str(), Some("invalid_json"));

        // 重叠冲突（片1 [0,16)，片2 [8,24) 内容不同）
        let _ = server.exchange(r#"{"op":"reset"}"#);
        server.exchange(
            &format!(r#"{{"op":"fragment","src":"1.1.1.1","dst":"2.2.2.2","protocol":6,"id":1,"offset":0,"mf":true,"payload_hex":"{}","now_ms":0}}"#,
                hex_encode(&[0x11; 16])),
        );
        let overlap = format!(
            r#"{{"op":"fragment","src":"1.1.1.1","dst":"2.2.2.2","protocol":6,"id":1,"offset":8,"mf":true,"payload_hex":"{}","now_ms":1}}"#,
            hex_encode(&[0x22; 16])
        );
        let resp = json::parse(&server.exchange(&overlap)).unwrap();
        assert_eq!(obj_get(&resp, "status").as_str(), Some("error"));
        assert_eq!(obj_get(&resp, "error").as_str(), Some("overlap_conflict"));
    }

    #[test]
    fn tcp_purge_with_virtual_clock() {
        let server = TestServer::start(100, 1 << 20);
        server.exchange(
            &format!(r#"{{"op":"fragment","src":"1.1.1.1","dst":"2.2.2.2","protocol":6,"id":2,"offset":0,"mf":true,"payload_hex":"{}","now_ms":0}}"#,
                hex_encode(&[0; 8])),
        );
        let resp = json::parse(&server.exchange(r#"{"op":"purge","now_ms":50}"#)).unwrap();
        assert_eq!(obj_get(&resp, "purged"), &Json::Array(vec![]));
        let resp = json::parse(&server.exchange(r#"{"op":"purge","now_ms":100}"#)).unwrap();
        let purged = obj_get(&resp, "purged");
        assert!(matches!(purged, Json::Array(a) if a.len() == 1));
        assert_eq!(
            obj_get(
                &json::parse(&server.exchange(r#"{"op":"status"}"#)).unwrap(),
                "pending_datagrams"
            )
            .as_u64(),
            Some(0)
        );
    }
}
