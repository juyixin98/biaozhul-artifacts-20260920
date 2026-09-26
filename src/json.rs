//! Minimal, dependency-free JSON support and the request/response model for
//! the command-line control entry point.
//!
//! Only the JSON subset this tool actually exchanges is supported, but the
//! parser is strict: trailing data, duplicate handling is left to the
//! extractor layer.

use std::collections::BTreeMap;

use crate::codec::{self, DecodeLimits};
use crate::error::{Error, Result};
use crate::set::IntSet;

// --------------------------------------------------------------------------
// JSON value model
// --------------------------------------------------------------------------

/// A small JSON value tree. Numbers are kept as `f64` (all values exchanged
/// here are exact: indices and limits are below 2^53).
#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

impl Json {
    fn type_name(&self) -> &'static str {
        match self {
            Json::Null => "null",
            Json::Bool(_) => "boolean",
            Json::Num(_) => "number",
            Json::Str(_) => "string",
            Json::Arr(_) => "array",
            Json::Obj(_) => "object",
        }
    }

    fn as_object(&self, ctx: &str) -> Result<&BTreeMap<String, Json>> {
        match self {
            Json::Obj(o) => Ok(o),
            other => Err(Error::BadRequest(format!(
                "{ctx} must be an object, got {}",
                other.type_name()
            ))),
        }
    }

    fn as_u32(&self, ctx: &str) -> Result<u32> {
        match self {
            Json::Num(n)
                if n.is_finite() && *n >= 0.0 && *n <= u32::MAX as f64 && n.fract() == 0.0 =>
            {
                Ok(*n as u32)
            }
            _ => Err(Error::BadRequest(format!(
                "{ctx} must be a non-negative integer <= 2^32-1"
            ))),
        }
    }

    fn as_u64(&self, ctx: &str) -> Result<u64> {
        match self {
            Json::Num(n)
                if n.is_finite() && *n >= 0.0 && *n <= u64::MAX as f64 && n.fract() == 0.0 =>
            {
                Ok(*n as u64)
            }
            _ => Err(Error::BadRequest(format!(
                "{ctx} must be a non-negative integer"
            ))),
        }
    }

    fn as_str(&self, ctx: &str) -> Result<&str> {
        match self {
            Json::Str(s) => Ok(s),
            other => Err(Error::BadRequest(format!(
                "{ctx} must be a string, got {}",
                other.type_name()
            ))),
        }
    }
}

// --------------------------------------------------------------------------
// Parser
// --------------------------------------------------------------------------

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(s: &'a str) -> Self {
        Parser {
            b: s.as_bytes(),
            pos: 0,
        }
    }

    fn parse_root(&mut self) -> Result<Json> {
        self.skip_ws();
        let v = self.parse_value()?;
        self.skip_ws();
        if self.pos != self.b.len() {
            return Err(Error::BadRequest(format!(
                "trailing data at byte {}",
                self.pos
            )));
        }
        Ok(v)
    }

    fn skip_ws(&mut self) {
        while self.pos < self.b.len() && matches!(self.b[self.pos], b' ' | b'\t' | b'\n' | b'\r') {
            self.pos += 1;
        }
    }

    fn parse_value(&mut self) -> Result<Json> {
        self.skip_ws();
        if self.pos >= self.b.len() {
            return Err(Error::BadRequest("unexpected end of JSON".into()));
        }
        match self.b[self.pos] {
            b'{' => self.parse_object(),
            b'[' => self.parse_array(),
            b'"' => Ok(Json::Str(self.parse_string()?)),
            b't' | b'f' => self.parse_bool(),
            b'n' => self.parse_null(),
            b'-' | b'0'..=b'9' => self.parse_number(),
            c => Err(Error::BadRequest(format!(
                "unexpected character {c:?} at byte {}",
                self.pos
            ))),
        }
    }

    fn parse_object(&mut self) -> Result<Json> {
        self.pos += 1; // {
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Obj(map));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(Error::BadRequest(format!(
                    "expected string key at byte {}",
                    self.pos
                )));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.next_byte() != Some(b':') {
                return Err(Error::BadRequest(format!(
                    "expected ':' at byte {}",
                    self.pos
                )));
            }
            let val = self.parse_value()?;
            if map.insert(key, val).is_some() {
                return Err(Error::BadRequest("duplicate object key".into()));
            }
            self.skip_ws();
            match self.next_byte() {
                Some(b',') => continue,
                Some(b'}') => break,
                _ => {
                    return Err(Error::BadRequest(format!(
                        "expected ',' or '}}' at byte {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Obj(map))
    }

    fn parse_array(&mut self) -> Result<Json> {
        self.pos += 1; // [
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Arr(items));
        }
        loop {
            items.push(self.parse_value()?);
            self.skip_ws();
            match self.next_byte() {
                Some(b',') => continue,
                Some(b']') => break,
                _ => {
                    return Err(Error::BadRequest(format!(
                        "expected ',' or ']' at byte {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Arr(items))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            match self.next_byte() {
                None => return Err(Error::BadRequest("unterminated string".into())),
                Some(b'"') => break,
                Some(b'\\') => {
                    let e = self
                        .next_byte()
                        .ok_or_else(|| Error::BadRequest("bad escape".into()))?;
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
                            let cp = if (0xD800..=0xDBFF).contains(&cp) {
                                // High surrogate; require \uXXXX low surrogate.
                                if self.next_byte() != Some(b'\\') || self.next_byte() != Some(b'u')
                                {
                                    return Err(Error::BadRequest("expected low surrogate".into()));
                                }
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(Error::BadRequest("bad low surrogate".into()));
                                }
                                0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                            } else {
                                cp
                            };
                            char::from_u32(cp)
                                .ok_or_else(|| Error::BadRequest("invalid unicode escape".into()))?
                                .encode_utf8(&mut [0; 4])
                                .chars()
                                .for_each(|c| out.push(c));
                        }
                        _ => return Err(Error::BadRequest("invalid escape".into())),
                    }
                }
                Some(c) if c < 0x20 => {
                    return Err(Error::BadRequest(format!(
                        "unescaped control byte 0x{c:02x}"
                    )))
                }
                Some(c) => {
                    // Accumulate raw UTF-8 continuation runs.
                    let start = self.pos - 1;
                    let extra = if c < 0x80 {
                        0
                    } else if c >> 5 == 0b110 {
                        1
                    } else if c >> 4 == 0b1110 {
                        2
                    } else if c >> 3 == 0b11110 {
                        3
                    } else {
                        return Err(Error::BadRequest(format!("bad UTF-8 lead byte at {start}")));
                    };
                    for _ in 0..extra {
                        let cc = self
                            .next_byte()
                            .ok_or_else(|| Error::BadRequest("short UTF-8".into()))?;
                        if cc >> 6 != 0b10 {
                            return Err(Error::BadRequest("bad UTF-8 continuation".into()));
                        }
                    }
                    out.push_str(
                        std::str::from_utf8(&self.b[start..self.pos])
                            .map_err(|_| Error::BadRequest("invalid UTF-8 in string".into()))?,
                    );
                }
            }
        }
        Ok(out)
    }

    fn parse_hex4(&mut self) -> Result<u32> {
        let mut v = 0u32;
        for _ in 0..4 {
            let c = self
                .next_byte()
                .ok_or_else(|| Error::BadRequest("short \\u escape".into()))?;
            let d = match c {
                b'0'..=b'9' => (c - b'0') as u32,
                b'a'..=b'f' => (c - b'a' + 10) as u32,
                b'A'..=b'F' => (c - b'A' + 10) as u32,
                _ => return Err(Error::BadRequest("bad hex digit".into())),
            };
            v = (v << 4) | d;
        }
        Ok(v)
    }

    fn parse_bool(&mut self) -> Result<Json> {
        if self.b[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.b[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err(Error::BadRequest(format!(
                "bad literal at byte {}",
                self.pos
            )))
        }
    }

    fn parse_null(&mut self) -> Result<Json> {
        if self.b[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(Error::BadRequest(format!(
                "bad literal at byte {}",
                self.pos
            )))
        }
    }

    fn parse_number(&mut self) -> Result<Json> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let mut saw_digit = false;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                saw_digit = true;
                self.pos += 1;
            } else {
                break;
            }
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            let frac_start = self.pos;
            while let Some(c) = self.peek() {
                if !c.is_ascii_digit() {
                    break;
                }
                self.pos += 1;
            }
            if self.pos == frac_start {
                return Err(Error::BadRequest(format!("bad number at byte {start}")));
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            let exp_start = self.pos;
            while let Some(c) = self.peek() {
                if !c.is_ascii_digit() {
                    break;
                }
                self.pos += 1;
            }
            if self.pos == exp_start {
                return Err(Error::BadRequest(format!("bad number at byte {start}")));
            }
        }
        if !saw_digit {
            return Err(Error::BadRequest(format!("bad number at byte {start}")));
        }
        // JSON forbids leading zeros ("01"); only "0" then '.', 'e' or end.
        let digit_start = start + usize::from(self.b[start] == b'-');
        if self.b.get(digit_start) == Some(&b'0')
            && matches!(self.b.get(digit_start + 1), Some(c) if c.is_ascii_digit())
        {
            return Err(Error::BadRequest(format!(
                "leading zero in number at byte {start}"
            )));
        }
        let text = std::str::from_utf8(&self.b[start..self.pos])
            .map_err(|_| Error::BadRequest("bad number encoding".into()))?;
        text.parse::<f64>()
            .map(Json::Num)
            .map_err(|_| Error::BadRequest(format!("bad number `{text}`")))
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    fn next_byte(&mut self) -> Option<u8> {
        let c = self.b.get(self.pos).copied();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }
}

/// Parse a JSON document strictly.
pub fn parse(input: &str) -> Result<Json> {
    Parser::new(input).parse_root()
}

// --------------------------------------------------------------------------
// Serialiser
// --------------------------------------------------------------------------

/// Render a [`Json`] value as a compact string.
pub fn stringify(v: &Json) -> String {
    let mut out = String::new();
    write_json(v, &mut out);
    out
}

fn write_json(v: &Json, out: &mut String) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Num(n) => {
            if n.fract() == 0.0 && n.is_finite() {
                out.push_str(&format!("{}", *n as u64));
            } else {
                out.push_str(&n.to_string());
            }
        }
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
            for (i, (k, val)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(k, out);
                out.push(':');
                write_json(val, out);
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

// --------------------------------------------------------------------------
// Base64 (standard alphabet, padded)
// --------------------------------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard padded base64 encoding.
pub fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64[((triple >> 18) & 0x3f) as usize] as char);
        out.push(B64[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64[((triple >> 6) & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(B64[(triple & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

/// Decode standard padded base64. Whitespace is rejected.
pub fn base64_decode(s: &str) -> Result<Vec<u8>> {
    fn val(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    if !bytes.len().is_multiple_of(4) {
        return Err(Error::BadBase64("length must be a multiple of 4".into()));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    let mut i = 0;
    while i < bytes.len() {
        let mut sextets = [0u8; 4];
        let mut pad = 0;
        for (j, slot) in sextets.iter_mut().enumerate() {
            let c = bytes[i + j];
            if c == b'=' {
                if i + j < bytes.len() - 2 {
                    return Err(Error::BadBase64("padding before the final quartet".into()));
                }
                pad += 1;
            } else if pad > 0 {
                return Err(Error::BadBase64("data after padding".into()));
            } else {
                *slot =
                    val(c).ok_or_else(|| Error::BadBase64(format!("illegal character {c:?}")))?;
            }
        }
        if pad > 2 {
            return Err(Error::BadBase64("too much padding".into()));
        }
        let triple = ((sextets[0] as u32) << 18)
            | ((sextets[1] as u32) << 12)
            | ((sextets[2] as u32) << 6)
            | sextets[3] as u32;
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad < 1 {
            out.push(triple as u8);
        }
        i += 4;
    }
    Ok(out)
}

// --------------------------------------------------------------------------
// Request model
// --------------------------------------------------------------------------

/// Default input-byte budget for decode requests that set none.
pub const DEFAULT_MAX_BYTES: u64 = 64 * 1024 * 1024;
/// Default decoded-value budget: the whole u32 universe minus one (the header
/// total field is u32).
pub const DEFAULT_MAX_VALUES: u64 = 1 << 32;

/// A set reference inside a request: either an explicit value list or an
/// already-encoded binary payload (base64).
fn load_set(field: &Json, name: &str, limits: DecodeLimits) -> Result<IntSet> {
    let obj = field.as_object(name)?;
    if let Some(Json::Str(b64)) = obj.get("data") {
        let raw = base64_decode(b64)?;
        return codec::decode_from_slice(&raw, limits);
    }
    match obj.get("values") {
        Some(Json::Arr(items)) => {
            let mut values = Vec::with_capacity(items.len());
            for it in items {
                values.push(it.as_u32(&format!("{name}.values[]"))?);
            }
            Ok(IntSet::from_values(&values))
        }
        Some(_) => Err(Error::BadRequest(format!("{name}.values must be an array"))),
        None => Err(Error::BadRequest(format!(
            "{name} must contain either \"values\" or base64 \"data\""
        ))),
    }
}

fn extract_limits(obj: &BTreeMap<String, Json>) -> DecodeLimits {
    let max_bytes = obj
        .get("max_bytes")
        .and_then(|j| j.as_u64("max_bytes").ok())
        .unwrap_or(DEFAULT_MAX_BYTES);
    let max_values = obj
        .get("max_values")
        .and_then(|j| j.as_u64("max_values").ok())
        .unwrap_or(DEFAULT_MAX_VALUES);
    DecodeLimits {
        max_bytes,
        max_values,
    }
}

fn set_summary(set: &IntSet) -> Vec<Json> {
    set.iter_chunks()
        .map(|(k, c)| {
            let mut m = BTreeMap::new();
            m.insert("key".into(), Json::Num(u32::from(*k) as f64));
            let kind = match c {
                crate::container::Container::Array(_) => "array",
                crate::container::Container::Bitmap(_) => "bitmap",
            };
            m.insert("kind".into(), Json::Str(kind.into()));
            m.insert("cardinality".into(), Json::Num(c.cardinality() as f64));
            Json::Obj(m)
        })
        .collect()
}

fn values_json(set: &IntSet) -> Json {
    Json::Arr(set.iter().map(|v| Json::Num(v as f64)).collect())
}

/// Execute one parsed request and return the response JSON.
pub fn dispatch(root: &Json) -> Json {
    match dispatch_result(root) {
        Ok(v) => v,
        Err(e) => {
            let mut m = BTreeMap::new();
            m.insert("ok".into(), Json::Bool(false));
            m.insert("error".into(), Json::Str(e.to_string()));
            Json::Obj(m)
        }
    }
}

fn ok(fields: Vec<(&str, Json)>) -> Json {
    let mut m = BTreeMap::new();
    m.insert("ok".into(), Json::Bool(true));
    for (k, v) in fields {
        m.insert(k.into(), v);
    }
    Json::Obj(m)
}

fn dispatch_result(root: &Json) -> Result<Json> {
    let obj = root.as_object("request")?;
    let op = obj
        .get("op")
        .ok_or_else(|| Error::BadRequest("missing \"op\"".into()))?
        .as_str("\"op\"")?;
    let limits = extract_limits(obj);

    match op {
        "encode" => {
            let set = load_set(
                obj.get("set").ok_or_else(|| Error::BadRequest("missing \"set\"".into()))?,
                "set",
                limits,
            )?;
            let raw = codec::encode_to_vec(&set)?;
            Ok(ok(vec![
                ("data", Json::Str(base64_encode(&raw))),
                ("bytes", Json::Num(raw.len() as f64)),
                ("cardinality", Json::Num(set.len() as f64)),
                ("chunks", Json::Num(set.chunk_count() as f64)),
            ]))
        }
        "decode" => {
            let b64 = obj
                .get("data")
                .ok_or_else(|| Error::BadRequest("missing \"data\"".into()))?
                .as_str("\"data\"")?;
            let raw = base64_decode(b64)?;
            let set = codec::decode_from_slice(&raw, limits)?;
            Ok(ok(vec![
                ("cardinality", Json::Num(set.len() as f64)),
                ("chunks", Json::Num(set.chunk_count() as f64)),
                ("containers", Json::Arr(set_summary(&set))),
                ("values", values_json(&set)),
            ]))
        }
        "info" => {
            let b64 = obj
                .get("data")
                .ok_or_else(|| Error::BadRequest("missing \"data\"".into()))?
                .as_str("\"data\"")?;
            let raw = base64_decode(b64)?;
            let set = codec::decode_from_slice(&raw, limits)?;
            Ok(ok(vec![
                ("bytes", Json::Num(raw.len() as f64)),
                ("cardinality", Json::Num(set.len() as f64)),
                ("chunks", Json::Num(set.chunk_count() as f64)),
                ("containers", Json::Arr(set_summary(&set))),
            ]))
        }
        "union" | "intersection" | "difference" => {
            let a = load_set(
                obj.get("a").ok_or_else(|| Error::BadRequest("missing \"a\"".into()))?,
                "a",
                limits,
            )?;
            let b = load_set(
                obj.get("b").ok_or_else(|| Error::BadRequest("missing \"b\"".into()))?,
                "b",
                limits,
            )?;
            let result = match op {
                "union" => a.union(&b),
                "intersection" => a.intersection(&b),
                "difference" => a.difference(&b),
                _ => unreachable!(),
            };
            let raw = codec::encode_to_vec(&result)?;
            Ok(ok(vec![
                ("operation", Json::Str(op.into())),
                ("data", Json::Str(base64_encode(&raw))),
                ("bytes", Json::Num(raw.len() as f64)),
                ("cardinality", Json::Num(result.len() as f64)),
                ("values", values_json(&result)),
            ]))
        }
        "contains" => {
            let set = load_set(
                obj.get("set").ok_or_else(|| Error::BadRequest("missing \"set\"".into()))?,
                "set",
                limits,
            )?;
            let value = obj
                .get("value")
                .ok_or_else(|| Error::BadRequest("missing \"value\"".into()))?
                .as_u32("\"value\"")?;
            Ok(ok(vec![
                ("value", Json::Num(value as f64)),
                ("contains", Json::Bool(set.contains(value))),
            ]))
        }
        other => Err(Error::BadRequest(format!(
            "unknown op \"{other}\" (expected encode|decode|info|union|intersection|difference|contains)"
        ))),
    }
}
