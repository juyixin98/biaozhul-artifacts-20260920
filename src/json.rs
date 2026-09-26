//! Minimal in-house JSON parser and writer used by the JSON control plane.
//!
//! Only the subset needed by this project is supported: objects, arrays,
//! strings (with `\u` escapes including surrogate pairs), integers, floats,
//! booleans and null. Object member order is preserved.

use crate::error::{Error, Result};
use std::io::Write;

/// A JSON value. Objects keep insertion order.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    String(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    /// Parse a complete JSON document (leading/trailing whitespace allowed).
    pub fn parse(input: &[u8]) -> Result<Json> {
        let mut p = Parser {
            bytes: input,
            pos: 0,
        };
        p.skip_ws();
        let v = p.parse_value()?;
        p.skip_ws();
        if p.pos != p.bytes.len() {
            return Err(Error::invalid(format!(
                "trailing garbage at byte {} in JSON document",
                p.pos
            )));
        }
        Ok(v)
    }

    /// Parse a JSON document from a string slice.
    pub fn parse_str(input: &str) -> Result<Json> {
        Json::parse(input.as_bytes())
    }

    /// Object field lookup.
    pub fn get(&self, key: &str) -> Option<&Json> {
        if let Json::Object(entries) = self {
            entries.iter().find(|(k, _)| k == key).map(|(_, v)| v)
        } else {
            None
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::String(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Array(a) => Some(a),
            _ => None,
        }
    }

    /// Integer field accepting JSON integers (and floats with integral value,
    /// so hand-written `1e6` is accepted too).
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Int(i) if *i >= 0 => Some(*i as u64),
            Json::Float(f) if f.is_finite() && f.fract() == 0.0 && *f >= 0.0 => Some(*f as u64),
            _ => None,
        }
    }
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

    fn parse_value(&mut self) -> Result<Json> {
        self.skip_ws();
        match self.peek() {
            None => Err(Error::truncated("JSON value")),
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(Json::String(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => Err(Error::invalid(format!(
                "unexpected byte {c:#x} at position {} in JSON",
                self.pos
            ))),
        }
    }

    fn parse_object(&mut self) -> Result<Json> {
        self.pos += 1; // '{'
        let mut entries = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(entries));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(Error::invalid(format!(
                    "expected string key in JSON object at byte {}",
                    self.pos
                )));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(Error::invalid("expected ':' after JSON object key"));
            }
            self.pos += 1;
            let value = self.parse_value()?;
            entries.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                other => {
                    return Err(Error::invalid(format!(
                        "expected ',' or '}}' in JSON object, found {other:?}"
                    )));
                }
            }
        }
        Ok(Json::Object(entries))
    }

    fn parse_array(&mut self) -> Result<Json> {
        self.pos += 1; // '['
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(items));
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
                other => {
                    return Err(Error::invalid(format!(
                        "expected ',' or ']' in JSON array, found {other:?}"
                    )));
                }
            }
        }
        Ok(Json::Array(items))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err(Error::truncated("JSON string")),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    match self.peek() {
                        Some(b'"') => out.push('"'),
                        Some(b'\\') => out.push('\\'),
                        Some(b'/') => out.push('/'),
                        Some(b'b') => out.push('\u{0008}'),
                        Some(b'f') => out.push('\u{000C}'),
                        Some(b'n') => out.push('\n'),
                        Some(b'r') => out.push('\r'),
                        Some(b't') => out.push('\t'),
                        Some(b'u') => {
                            self.pos += 1;
                            let cp = self.parse_hex4()?;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                // High surrogate; require a low surrogate.
                                if self.peek() != Some(b'\\') {
                                    return Err(Error::invalid("bad UTF-16 surrogate pair"));
                                }
                                self.pos += 1;
                                if self.peek() != Some(b'u') {
                                    return Err(Error::invalid("bad UTF-16 surrogate pair"));
                                }
                                self.pos += 1;
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(Error::invalid("bad UTF-16 low surrogate"));
                                }
                                let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                out.push(char::from_u32(c).ok_or_else(|| {
                                    Error::invalid("invalid scalar from surrogate pair")
                                })?);
                                continue;
                            } else if (0xDC00..=0xDFFF).contains(&cp) {
                                return Err(Error::invalid("unexpected UTF-16 low surrogate"));
                            } else {
                                out.push(
                                    char::from_u32(cp)
                                        .ok_or_else(|| Error::invalid("invalid unicode escape"))?,
                                );
                                continue;
                            }
                        }
                        other => {
                            return Err(Error::invalid(format!("bad escape \\{other:?}")));
                        }
                    }
                    self.pos += 1;
                }
                Some(c) if c < 0x20 => {
                    return Err(Error::invalid(format!(
                        "unescaped control byte {c:#x} in JSON string"
                    )));
                }
                Some(_) => {
                    // Copy a run of raw UTF-8 bytes up to the next escape/quote.
                    let start = self.pos;
                    while self.pos < self.bytes.len()
                        && !matches!(self.bytes[self.pos], b'"' | b'\\')
                        && self.bytes[self.pos] >= 0x20
                    {
                        self.pos += 1;
                    }
                    let chunk = &self.bytes[start..self.pos];
                    match std::str::from_utf8(chunk) {
                        Ok(s) => out.push_str(s),
                        Err(_) => return Err(Error::invalid("invalid UTF-8 in JSON string")),
                    }
                }
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u32> {
        if self.pos + 4 > self.bytes.len() {
            return Err(Error::truncated("\\u escape"));
        }
        let mut v = 0u32;
        for _ in 0..4 {
            let c = self.bytes[self.pos];
            let d = (c as char)
                .to_digit(16)
                .ok_or_else(|| Error::invalid(format!("bad hex digit {c:#?} in \\u escape")))?;
            v = v * 16 + d;
            self.pos += 1;
        }
        Ok(v)
    }

    fn parse_bool(&mut self) -> Result<Json> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err(Error::invalid("invalid JSON literal"))
        }
    }

    fn parse_null(&mut self) -> Result<Json> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(Error::invalid("invalid JSON literal"))
        }
    }

    fn parse_number(&mut self) -> Result<Json> {
        let start = self.pos;
        let mut is_float = false;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        // Integer part: "0" alone, or 1-9 followed by digits.
        match self.peek() {
            Some(b'0') => {
                self.pos += 1;
            }
            Some(c) if c.is_ascii_digit() => {
                self.pos += 1;
                while let Some(c) = self.peek() {
                    if c.is_ascii_digit() {
                        self.pos += 1;
                    } else {
                        break;
                    }
                }
            }
            _ => return Err(Error::invalid("invalid JSON number: expected digit")),
        }
        // Optional fraction.
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(Error::invalid(
                    "invalid JSON number: digits expected after '.'",
                ));
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        // Optional exponent.
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                return Err(Error::invalid(
                    "invalid JSON number: digits expected in exponent",
                ));
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| Error::invalid("bad JSON number"))?;
        if is_float {
            text.parse::<f64>()
                .map(Json::Float)
                .map_err(|_| Error::invalid(format!("bad JSON number {text:?}")))
        } else {
            text.parse::<i64>()
                .map(Json::Int)
                .map_err(|_| Error::invalid(format!("JSON integer out of range: {text:?}")))
        }
    }
}

/// Write `s` as a quoted, escaped JSON string.
pub fn write_json_string<W: Write + ?Sized>(w: &mut W, s: &str) -> std::io::Result<()> {
    w.write_all(b"\"")?;
    let bytes = s.as_bytes();
    let mut run_start = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let escape: Option<&[u8]> = match b {
            b'"' => Some(b"\\\""),
            b'\\' => Some(b"\\\\"),
            b'\n' => Some(b"\\n"),
            b'\r' => Some(b"\\r"),
            b'\t' => Some(b"\\t"),
            0x08 => Some(b"\\b"),
            0x0c => Some(b"\\f"),
            c if c < 0x20 => None, // handled via \u00xx below
            _ => None,
        };
        if escape.is_some() || b < 0x20 {
            w.write_all(&bytes[run_start..i])?;
            match escape {
                Some(e) => w.write_all(e)?,
                None => write!(w, "\\u{b:04x}")?,
            }
            run_start = i + 1;
        }
    }
    w.write_all(&bytes[run_start..])?;
    w.write_all(b"\"")
}

/// Serialize a [`Json`] value.
pub fn write_json_value<W: Write + ?Sized>(w: &mut W, v: &Json) -> std::io::Result<()> {
    match v {
        Json::Null => w.write_all(b"null"),
        Json::Bool(true) => w.write_all(b"true"),
        Json::Bool(false) => w.write_all(b"false"),
        Json::Int(i) => write!(w, "{i}"),
        Json::Float(f) => write!(w, "{f}"),
        Json::String(s) => write_json_string(w, s),
        Json::Array(items) => {
            w.write_all(b"[")?;
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    w.write_all(b",")?;
                }
                write_json_value(w, item)?;
            }
            w.write_all(b"]")
        }
        Json::Object(entries) => {
            w.write_all(b"{")?;
            for (i, (k, val)) in entries.iter().enumerate() {
                if i > 0 {
                    w.write_all(b",")?;
                }
                write_json_string(w, k)?;
                w.write_all(b":")?;
                write_json_value(w, val)?;
            }
            w.write_all(b"}")
        }
    }
}

/// Serialize to a `String`.
pub fn to_string(v: &Json) -> String {
    let mut buf = Vec::new();
    write_json_value(&mut buf, v).expect("writing to a Vec cannot fail");
    String::from_utf8(buf).expect("JSON output is valid UTF-8")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_scalars_and_containers() {
        assert_eq!(Json::parse_str("null").unwrap(), Json::Null);
        assert_eq!(Json::parse_str("true").unwrap(), Json::Bool(true));
        assert_eq!(Json::parse_str("-42").unwrap(), Json::Int(-42));
        let v = Json::parse_str(r#"{"a": 1, "b": ["x", null, true]}"#).unwrap();
        assert_eq!(v.get("a").unwrap(), &Json::Int(1));
        assert_eq!(v.get("b").unwrap().as_array().unwrap().len(), 3);
    }

    #[test]
    fn parse_unicode_and_surrogates() {
        let v = Json::parse_str(r#""中 🙂 é 😀""#).unwrap();
        assert_eq!(v.as_str().unwrap(), "中 🙂 é 😀");
    }

    #[test]
    fn rejects_bad_inputs() {
        assert!(Json::parse_str("").is_err());
        assert!(Json::parse_str("01").is_err());
        assert!(Json::parse_str("[1,]").is_err());
        assert!(Json::parse_str(r#""\ud800""#).is_err()); // lone high surrogate
        assert!(Json::parse_str("{\"a\":1}x").is_err());
    }

    #[test]
    fn string_writer_escapes() {
        let mut buf = Vec::new();
        write_json_string(&mut buf, "a\"b\\c\n\t\u{1}中").unwrap();
        // Raw string: JSON escaping must emit literal backslash sequences.
        assert_eq!(
            std::str::from_utf8(&buf).unwrap(),
            r#""a\"b\\c\n\t\u0001中""#
        );
    }

    #[test]
    fn string_roundtrip_through_writer() {
        let original = "he said \"hi\"\nline2\t\\end 🙂";
        let mut buf = Vec::new();
        write_json_string(&mut buf, original).unwrap();
        let parsed = Json::parse(&buf).unwrap();
        assert_eq!(parsed.as_str().unwrap(), original);
    }
}
