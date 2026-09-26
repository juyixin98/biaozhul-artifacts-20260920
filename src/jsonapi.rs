//! JSON 控制入口。
//!
//! 零外部依赖：自带一个够用的 JSON 解析器、Base64 与 Hex 编解码器。
//!
//! # 请求格式
//!
//! ```json
//! {
//!   "op": "compress" | "decompress",
//!   "data": "<base64 或 hex 字符串>",
//!   "encoding": "base64" | "hex",          // 可选，默认 base64
//!   "block_size": 1048576,                  // 可选，压缩用
//!   "max_output_bytes": 1073741824,         // 可选，解压用
//!   "max_block_bytes": 16777216,            // 可选，解压用
//!   "incomplete_policy": "reject" | "allow" // 可选，默认 reject
//! }
//! ```
//!
//! # 响应格式
//!
//! 成功：`{"ok":true,"op":...,"result":{...}}`；
//! 失败：`{"ok":false,"error":{"kind":"...","message":"..."}}`。

use crate::error::{Error, Result};
use crate::stream::{
    compress_stream, decompress_stream, CompressStats, DecompressStats, IncompletePolicy, Limits,
    DEFAULT_BLOCK_SIZE,
};

// ---------------------------------------------------------------------------
// 极简 JSON 值模型与解析器
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub enum JsonValue {
    Null,
    Bool(bool),
    /// 数值同时保留文本形式，便于无损转 u64。
    Num(f64, String),
    Str(String),
    Arr(Vec<JsonValue>),
    /// 保留插入顺序。
    Obj(Vec<(String, JsonValue)>),
}

impl JsonValue {
    fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::Str(s) => Some(s),
            _ => None,
        }
    }

    fn as_u64(&self) -> Option<u64> {
        match self {
            JsonValue::Num(_, raw) => raw.parse::<u64>().ok(),
            _ => None,
        }
    }

    /// 按键查找（仅对象有效）。
    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        match self {
            JsonValue::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

/// 解析 JSON 文本。
pub fn parse_json(text: &str) -> Result<JsonValue> {
    let bytes = text.as_bytes();
    let mut p = Parser { bytes, pos: 0 };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != bytes.len() {
        return Err(Error::BadRequest("trailing characters after JSON".into()));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn parse_value(&mut self) -> Result<JsonValue> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(JsonValue::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            _ => Err(Error::BadRequest("unexpected JSON token".into())),
        }
    }

    fn parse_object(&mut self) -> Result<JsonValue> {
        self.pos += 1; // {
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonValue::Obj(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(Error::BadRequest("expected string key".into()));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(Error::BadRequest("expected ':'".into()));
            }
            self.pos += 1;
            let value = self.parse_value()?;
            pairs.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(Error::BadRequest("expected ',' or '}'".into())),
            }
        }
        Ok(JsonValue::Obj(pairs))
    }

    fn parse_array(&mut self) -> Result<JsonValue> {
        self.pos += 1; // [
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(JsonValue::Arr(items));
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
                _ => return Err(Error::BadRequest("expected ',' or ']'".into())),
            }
        }
        Ok(JsonValue::Arr(items))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1 // 开引号
        ;
        let mut out = String::new();
        loop {
            let c = *self
                .bytes
                .get(self.pos)
                .ok_or_else(|| Error::BadRequest("unterminated string".into()))?;
            self.pos += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = *self
                        .bytes
                        .get(self.pos)
                        .ok_or_else(|| Error::BadRequest("bad escape".into()))?;
                    self.pos += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000C}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let cp = self.parse_hex4()?;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                // 高代理项，必须跟 \uXXXX 低代理项。
                                if self.bytes.get(self.pos..self.pos + 2) != Some(b"\\u".as_slice())
                                {
                                    return Err(Error::BadRequest("bad surrogate pair".into()));
                                }
                                self.pos += 2;
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(Error::BadRequest("bad low surrogate".into()));
                                }
                                let c =
                                    0x10000 + (((cp - 0xD800) as u32) << 10) + (lo - 0xDC00) as u32;
                                out.push(char::from_u32(c).unwrap());
                            } else if (0xDC00..=0xDFFF).contains(&cp) {
                                return Err(Error::BadRequest("lone low surrogate".into()));
                            } else {
                                out.push(char::from_u32(cp as u32).unwrap());
                            }
                        }
                        _ => return Err(Error::BadRequest("bad escape character".into())),
                    }
                }
                _ => {
                    // 原样保留 UTF-8 字节：收集连续的普通字节段后用 from_utf8。
                    let start = self.pos - 1;
                    while self.pos < self.bytes.len()
                        && self.bytes[self.pos] != b'"'
                        && self.bytes[self.pos] != b'\\'
                    {
                        self.pos += 1;
                    }
                    out.push_str(
                        std::str::from_utf8(&self.bytes[start..self.pos])
                            .map_err(|_| Error::BadRequest("invalid UTF-8 in string".into()))?,
                    );
                }
            }
        }
        Ok(out)
    }

    fn parse_hex4(&mut self) -> Result<u16> {
        if self.pos + 4 > self.bytes.len() {
            return Err(Error::BadRequest("short \\u escape".into()));
        }
        let s = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
            .map_err(|_| Error::BadRequest("bad \\u escape".into()))?;
        let v = u16::from_str_radix(s, 16)
            .map_err(|_| Error::BadRequest("bad \\u hex digits".into()))?;
        self.pos += 4;
        Ok(v)
    }

    fn parse_bool(&mut self) -> Result<JsonValue> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(JsonValue::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(JsonValue::Bool(false))
        } else {
            Err(Error::BadRequest("invalid literal".into()))
        }
    }

    fn parse_null(&mut self) -> Result<JsonValue> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(JsonValue::Null)
        } else {
            Err(Error::BadRequest("invalid literal".into()))
        }
    }

    fn parse_number(&mut self) -> Result<JsonValue> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while self.pos < self.bytes.len()
            && matches!(
                self.bytes[self.pos],
                b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-'
            )
        {
            self.pos += 1;
        }
        let raw = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| Error::BadRequest("bad number".into()))?;
        let f: f64 = raw
            .parse()
            .map_err(|_| Error::BadRequest("bad number".into()))?;
        Ok(JsonValue::Num(f, raw.to_string()))
    }
}

// ---------------------------------------------------------------------------
// JSON 字符串转义输出
// ---------------------------------------------------------------------------

fn json_escape(s: &str, out: &mut String) {
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
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------------------
// Base64（标准字母表，带填充；解码容错：忽略缺失填充，拒绝非法字符）
// ---------------------------------------------------------------------------

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Base64 编码。
pub fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[((triple >> 18) & 63) as usize] as char);
        out.push(B64_ALPHABET[((triple >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64_ALPHABET[((triple >> 6) & 63) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(B64_ALPHABET[(triple & 63) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

/// Base64 解码。允许换行/空白；填充可省略，但一旦出现 `=` 其后只能是 `=`。
pub fn base64_decode(s: &str) -> Result<Vec<u8>> {
    let mut table = [255u8; 256];
    for (i, &c) in B64_ALPHABET.iter().enumerate() {
        table[c as usize] = i as u8;
    }
    let mut out = Vec::new();
    let mut acc: u32 = 0;
    let mut bits = 0u32;
    let mut n_data = 0usize;
    let mut padding = 0u32;
    for &b in s.as_bytes() {
        if matches!(b, b' ' | b'\t' | b'\n' | b'\r') {
            continue;
        }
        if b == b'=' {
            padding += 1;
            continue;
        }
        if padding > 0 {
            return Err(Error::BadRequest("base64: data after padding".into()));
        }
        let v = table[b as usize];
        if v == 255 {
            return Err(Error::BadRequest("base64: invalid character".into()));
        }
        n_data += 1;
        acc = (acc << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }

    // 末尾量子的余数：0 个数据字符→无剩余；2→应有 "=="，输出 1 字节，
    // 残留 4 位必须为 0；3→应有 "="，输出 2 字节，残留 2 位必须为 0。
    match n_data % 4 {
        0 => {
            if padding != 0 || bits != 0 {
                return Err(Error::BadRequest("base64: bad quantum".into()));
            }
        }
        2 => {
            if padding > 2 || bits != 4 || acc != 0 {
                return Err(Error::BadRequest("base64: bad final quantum".into()));
            }
        }
        3 => {
            if padding > 1 || bits != 2 || acc != 0 {
                return Err(Error::BadRequest("base64: bad final quantum".into()));
            }
        }
        _ => return Err(Error::BadRequest("base64: truncated quantum".into())),
    }
    Ok(out)
}

/// Hex 编码（小写）。
pub fn hex_encode(data: &[u8]) -> String {
    let mut s = String::with_capacity(data.len() * 2);
    for b in data {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Hex 解码（大小写皆可，必须偶数个字符）。
pub fn hex_decode(s: &str) -> Result<Vec<u8>> {
    if s.len() % 2 != 0 {
        return Err(Error::BadRequest("hex: odd length".into()));
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(s.len() / 2);
    let val = |c: u8| -> Result<u8> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(Error::BadRequest("hex: invalid character".into())),
        }
    };
    for pair in bytes.chunks(2) {
        out.push((val(pair[0])? << 4) | val(pair[1])?);
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// 请求处理
// ---------------------------------------------------------------------------

/// 输入/输出字节的文本编码。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataEncoding {
    Base64,
    Hex,
}

impl DataEncoding {
    fn encode(&self, data: &[u8]) -> String {
        match self {
            DataEncoding::Base64 => base64_encode(data),
            DataEncoding::Hex => hex_encode(data),
        }
    }
    fn decode(&self, s: &str) -> Result<Vec<u8>> {
        match self {
            DataEncoding::Base64 => base64_decode(s),
            DataEncoding::Hex => hex_decode(s),
        }
    }
}

fn err_kind(e: &Error) -> &'static str {
    match e {
        Error::LimitExceeded { .. } => "limit_exceeded",
        Error::BadMagic => "bad_magic",
        Error::UnsupportedVersion(_) => "unsupported_version",
        Error::TruncatedHeader => "truncated_header",
        Error::TruncatedStream => "truncated_stream",
        Error::ReservedBitsSet(_) => "reserved_bits_set",
        Error::OutputTooLong { .. } => "output_too_long",
        Error::InvalidLengthTable(_) => "invalid_length_table",
        Error::Oversubscribed => "oversubscribed",
        Error::IncompleteCode => "incomplete_code",
        Error::LengthMismatch { .. } => "length_mismatch",
        Error::UnexpectedEndOfCode => "unexpected_end_of_code",
        Error::UndefinedCodeword => "undefined_codeword",
        Error::TrailingBits => "trailing_bits",
        Error::TrailingData => "trailing_data",
        Error::NonZeroPadding => "non_zero_padding",
        Error::CodeDoesNotFit => "code_does_not_fit",
        Error::FrequencyLogic(_) => "frequency_logic",
        Error::EmptyInput => "empty_input",
        Error::BadRequest(_) => "bad_request",
        Error::Io(_) => "io_error",
    }
}

fn error_response(e: &Error) -> String {
    let mut msg = String::new();
    json_escape(&e.to_string(), &mut msg);
    format!(
        "{{\"ok\":false,\"error\":{{\"kind\":\"{}\",\"message\":{}}}}}",
        err_kind(e),
        msg
    )
}

fn require_object(v: &JsonValue) -> Result<&Vec<(String, JsonValue)>> {
    match v {
        JsonValue::Obj(pairs) => Ok(pairs),
        _ => Err(Error::BadRequest("request must be a JSON object".into())),
    }
}

/// 处理一条 JSON 请求，返回 JSON 响应文本。
pub fn handle_request(text: &str) -> String {
    match handle_request_inner(text) {
        Ok(s) => s,
        Err(e) => error_response(&e),
    }
}

fn handle_request_inner(text: &str) -> Result<String> {
    let req = parse_json(text)?;
    require_object(&req)?;

    let op = req
        .get("op")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::BadRequest("missing string field 'op'".into()))?;

    let encoding = match req.get("encoding").and_then(|v| v.as_str()) {
        None | Some("base64") => DataEncoding::Base64,
        Some("hex") => DataEncoding::Hex,
        Some(other) => {
            return Err(Error::BadRequest(format!(
                "encoding must be base64 or hex, got {other}"
            )))
        }
    };

    let data_str = req
        .get("data")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::BadRequest("missing string field 'data'".into()))?;
    let data = encoding.decode(data_str)?;

    match op {
        "compress" => {
            let block_size = match req.get("block_size") {
                None => DEFAULT_BLOCK_SIZE,
                Some(v) => {
                    let n = v
                        .as_u64()
                        .ok_or_else(|| Error::BadRequest("block_size must be u64".into()))?;
                    if !(1..=16_777_216).contains(&n) {
                        return Err(Error::BadRequest(
                            "block_size must be in 1..=16777216".into(),
                        ));
                    }
                    n as usize
                }
            };
            let mut src = std::io::Cursor::new(&data);
            let mut compressed = Vec::with_capacity(data.len() / 2);
            let stats = compress_stream(&mut src, &mut compressed, block_size)?;
            Ok(render_compress(&compressed, encoding, &stats))
        }
        "decompress" => {
            let mut limits = Limits::default();
            if let Some(v) = req.get("max_output_bytes") {
                limits.max_output_bytes = v
                    .as_u64()
                    .ok_or_else(|| Error::BadRequest("max_output_bytes must be u64".into()))?;
            }
            if let Some(v) = req.get("max_block_bytes") {
                limits.max_block_bytes = v
                    .as_u64()
                    .ok_or_else(|| Error::BadRequest("max_block_bytes must be u64".into()))?;
            }
            let policy = match req.get("incomplete_policy").and_then(|v| v.as_str()) {
                None | Some("reject") => IncompletePolicy::Reject,
                Some("allow") => IncompletePolicy::Allow,
                Some(other) => {
                    return Err(Error::BadRequest(format!(
                        "incomplete_policy must be reject or allow, got {other}"
                    )))
                }
            };
            let mut src = std::io::Cursor::new(&data);
            let mut raw = Vec::new();
            let stats = decompress_stream(&mut src, &mut raw, &limits, policy)?;
            Ok(render_decompress(&raw, encoding, &stats))
        }
        other => Err(Error::BadRequest(format!("unknown op '{other}'"))),
    }
}

fn render_compress(data: &[u8], enc: DataEncoding, s: &CompressStats) -> String {
    let enc_name = match enc {
        DataEncoding::Base64 => "base64",
        DataEncoding::Hex => "hex",
    };
    let payload = enc.encode(data);
    let mut payload_json = String::new();
    json_escape(&payload, &mut payload_json);
    format!(
        "{{\"ok\":true,\"op\":\"compress\",\"result\":{{\"encoding\":\"{enc_name}\",\"data\":{payload_json},\"stats\":{{\"input_bytes\":{},\"output_bytes\":{},\"blocks\":{},\"distinct_symbols_total\":{},\"ratio\":{:.6}}}}}}}",
        s.input_bytes, s.output_bytes, s.blocks, s.distinct_symbols_total, s.ratio()
    )
}

fn render_decompress(data: &[u8], enc: DataEncoding, s: &DecompressStats) -> String {
    let enc_name = match enc {
        DataEncoding::Base64 => "base64",
        DataEncoding::Hex => "hex",
    };
    let payload = enc.encode(data);
    let mut payload_json = String::new();
    json_escape(&payload, &mut payload_json);
    format!(
        "{{\"ok\":true,\"op\":\"decompress\",\"result\":{{\"encoding\":\"{enc_name}\",\"data\":{payload_json},\"stats\":{{\"compressed_bytes\":{},\"output_bytes\":{},\"blocks\":{}}}}}}}",
        s.compressed_bytes, s.output_bytes, s.blocks
    )
}
