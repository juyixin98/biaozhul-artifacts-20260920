//! Minimal, dependency-free JSON parser and serializer.
//!
//! Numbers are retained with integer precision ([`Json::Int`] /
//! [`Json::UInt`]) so that `int32`/`int64`/fixed64 fields round-trip exactly
//! even above 2^53; fractional or exponential forms become [`Json::Float`].

use std::fmt::Write as _;

use crate::error::{Error, Result};

/// A JSON value. Objects are represented by an ordered map (insertion order
/// preserved) rather than a BTreeMap so field declaration order is stable.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// Negative signed integer literal.
    Int(i64),
    /// Non-negative integer literal (kept separate so u64 values up to
    /// 2^64-1 are representable).
    UInt(u64),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&Vec<(String, Json)>> {
        match self {
            Json::Object(o) => Some(o),
            _ => None,
        }
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object()?
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v)
    }

    /// Any JSON number as i64 if it is an integral literal in range.
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => Some(*i),
            Json::UInt(u) if *u <= i64::MAX as u64 => Some(*u as i64),
            _ => None,
        }
    }

    /// Any JSON number as u64 if it is an integral literal in range.
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::UInt(u) => Some(*u),
            Json::Int(i) if *i >= 0 => Some(*i as u64),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Float(f) => Some(*f),
            Json::Int(i) => Some(*i as f64),
            Json::UInt(u) => Some(*u as f64),
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
}

/// Parse a JSON document; trailing non-whitespace is rejected.
pub fn parse(input: &str) -> Result<Json> {
    let mut p = Parser {
        b: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.b.len() {
        return Err(p.err("trailing characters after JSON value"));
    }
    Ok(v)
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> Error {
        Error::Json(format!("at byte {}: {msg}", self.pos))
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while let Some(c) = self.peek() {
            if matches!(c, b' ' | b'\t' | b'\n' | b'\r') {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn parse_value(&mut self) -> Result<Json> {
        self.skip_ws();
        match self.peek() {
            None => Err(self.err("unexpected end of input")),
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(Json::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => Err(self.err(&format!("unexpected character '{}'", c as char))),
        }
    }

    fn parse_object(&mut self) -> Result<Json> {
        self.pos += 1; // {
        let mut out = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(out));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("expected string key in object"));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(self.err("expected ':' after object key"));
            }
            self.pos += 1;
            let val = self.parse_value()?;
            out.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or '}' in object")),
            }
        }
        Ok(Json::Object(out))
    }

    fn parse_array(&mut self) -> Result<Json> {
        self.pos += 1; // [
        let mut out = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(out));
        }
        loop {
            out.push(self.parse_value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or ']' in array")),
            }
        }
        Ok(Json::Array(out))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err(self.err("unterminated string")),
                Some(b'"') => {
                    self.pos += 1;
                    break;
                }
                Some(b'\\') => {
                    self.pos += 1;
                    match self.peek() {
                        Some(b'"') => out.push('"'),
                        Some(b'\\') => out.push('\\'),
                        Some(b'/') => out.push('/'),
                        Some(b'n') => out.push('\n'),
                        Some(b't') => out.push('\t'),
                        Some(b'r') => out.push('\r'),
                        Some(b'b') => out.push('\u{8}'),
                        Some(b'f') => out.push('\u{c}'),
                        Some(b'u') => {
                            self.pos += 1;
                            let cp1 = self.parse_hex4()?;
                            let cp = if (0xD800..=0xDBFF).contains(&cp1) {
                                // High surrogate; require low surrogate.
                                if self.peek() != Some(b'\\') {
                                    return Err(self.err("bad UTF-16 surrogate pair"));
                                }
                                self.pos += 1;
                                if self.peek() != Some(b'u') {
                                    return Err(self.err("bad UTF-16 surrogate pair"));
                                }
                                self.pos += 1;
                                let cp2 = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&cp2) {
                                    return Err(self.err("bad UTF-16 surrogate pair"));
                                }
                                0x10000 + (((cp1 - 0xD800) as u32) << 10) + (cp2 - 0xDC00) as u32
                            } else if (0xDC00..=0xDFFF).contains(&cp1) {
                                return Err(self.err("lone low surrogate"));
                            } else {
                                cp1 as u32
                            };
                            match char::from_u32(cp) {
                                Some(c) => out.push(c),
                                None => return Err(self.err("invalid unicode escape")),
                            }
                            continue;
                        }
                        _ => return Err(self.err("invalid escape sequence")),
                    }
                    self.pos += 1;
                }
                Some(c) if c < 0x80 => {
                    out.push(c as char);
                    self.pos += 1;
                }
                Some(_) => {
                    // Multibyte UTF-8: validate by copying the whole run.
                    let start = self.pos;
                    let width = utf8_width(self.b[start]);
                    if width == 0 || start + width > self.b.len() {
                        return Err(self.err("invalid UTF-8 in string"));
                    }
                    let slice = &self.b[start..start + width];
                    match std::str::from_utf8(slice) {
                        Ok(s) => out.push_str(s),
                        Err(_) => return Err(self.err("invalid UTF-8 in string")),
                    }
                    self.pos += width;
                }
            }
        }
        Ok(out)
    }

    fn parse_hex4(&mut self) -> Result<u16> {
        let mut v: u16 = 0;
        for _ in 0..4 {
            let c = self.peek().ok_or_else(|| self.err("short \\u escape"))?;
            let d = (c as char)
                .to_digit(16)
                .ok_or_else(|| self.err("bad hex digit in \\u escape"))? as u16;
            v = v * 16 + d;
            self.pos += 1;
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
            Err(self.err("invalid literal"))
        }
    }

    fn parse_null(&mut self) -> Result<Json> {
        if self.b[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(self.err("invalid literal"))
        }
    }

    fn parse_number(&mut self) -> Result<Json> {
        let start = self.pos;
        let mut is_float = false;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while let Some(c) = self.peek() {
            match c {
                b'0'..=b'9' => self.pos += 1,
                b'.' | b'e' | b'E' | b'+' | b'-' => {
                    is_float = true;
                    self.pos += 1;
                }
                _ => break,
            }
        }
        let text =
            std::str::from_utf8(&self.b[start..self.pos]).map_err(|_| self.err("bad number"))?;
        if is_float {
            let f: f64 = text
                .parse()
                .map_err(|_| self.err(&format!("invalid number `{text}`")))?;
            Ok(Json::Float(f))
        } else if text.starts_with('-') {
            let i: i64 = text
                .parse()
                .map_err(|_| self.err(&format!("integer out of i64 range: `{text}`")))?;
            Ok(Json::Int(i))
        } else {
            let u: u64 = text
                .parse()
                .map_err(|_| self.err(&format!("integer out of u64 range: `{text}`")))?;
            Ok(Json::UInt(u))
        }
    }
}

fn utf8_width(first: u8) -> usize {
    match first {
        0x00..=0x7F => 1,
        0xC0..=0xDF => 2,
        0xE0..=0xEF => 3,
        0xF0..=0xF7 => 4,
        _ => 0,
    }
}

// ---------------------------------------------------------------------------
// Serializer
// ---------------------------------------------------------------------------

/// Serialize with two-space indentation.
pub fn to_string_pretty(v: &Json) -> Result<String> {
    let mut out = String::new();
    write_pretty(v, &mut out, 0)?;
    out.push('\n');
    Ok(out)
}

fn write_pretty(v: &Json, out: &mut String, indent: usize) -> Result<()> {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Int(i) => {
            write!(out, "{i}").unwrap();
        }
        Json::UInt(u) => {
            write!(out, "{u}").unwrap();
        }
        Json::Float(f) => {
            if f.is_finite() {
                // Rust's Debug formatting is a shortest round-trip repr and
                // always contains either a '.' or an exponent.
                write!(out, "{f:?}").unwrap();
            } else {
                // JSON has no NaN/Infinity; emit null so output stays valid.
                out.push_str("null");
            }
        }
        Json::Str(s) => write_json_string(s, out),
        Json::Array(a) => {
            if a.is_empty() {
                out.push_str("[]");
            } else {
                out.push_str("[\n");
                for (i, item) in a.iter().enumerate() {
                    push_indent(out, indent + 1);
                    write_pretty(item, out, indent + 1)?;
                    if i + 1 < a.len() {
                        out.push(',');
                    }
                    out.push('\n');
                }
                push_indent(out, indent);
                out.push(']');
            }
        }
        Json::Object(o) => {
            if o.is_empty() {
                out.push_str("{}");
            } else {
                out.push_str("{\n");
                for (i, (k, val)) in o.iter().enumerate() {
                    push_indent(out, indent + 1);
                    write_json_string(k, out);
                    out.push_str(": ");
                    write_pretty(val, out, indent + 1)?;
                    if i + 1 < o.len() {
                        out.push(',');
                    }
                    out.push('\n');
                }
                push_indent(out, indent);
                out.push('}');
            }
        }
    }
    Ok(())
}

fn push_indent(out: &mut String, n: usize) {
    for _ in 0..n {
        out.push_str("  ");
    }
}

fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                write!(out, "\\u{:04x}", c as u32).unwrap();
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Build a JSON object from key/value pairs (macro-free helper used by the CLI).
pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
    Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_basic() {
        let docs = [
            "null",
            "true",
            "false",
            "  123 ",
            "-456",
            "1.5e3",
            "\"hi\\n\\u00e9\"",
            "[1, 2, [3], {}]",
            "{\"a\": 1, \"b\": [true, null]}",
        ];
        for d in docs {
            let v = parse(d).unwrap();
            let s = to_string_pretty(&v).unwrap();
            parse(&s).unwrap_or_else(|e| panic!("reparse failed for {d:?} -> {s}: {e}"));
        }
    }

    #[test]
    fn integer_precision() {
        let v = parse("9007199254740993").unwrap(); // 2^53+1
        assert_eq!(v.as_u64(), Some(9_007_199_254_740_993));
        let v = parse("-9223372036854775808").unwrap();
        assert_eq!(v.as_i64(), Some(i64::MIN));
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse("").is_err());
        assert!(parse("123abc").is_err());
        assert!(parse("{\"a\":}").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse("\"unterminated").is_err());
        assert!(parse("[1, 2").is_err());
    }
}
