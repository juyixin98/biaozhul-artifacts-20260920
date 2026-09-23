//! Minimal JSON parser/serializer, dependency-free.
//! Sufficient for this project's HTTP API; not a general-purpose JSON library.

use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Json {
    Null,
    Bool(bool),
    /// Parsed integer; this project never serializes/parses non-integer JSON numbers.
    Int(i64),
    Str(String),
    Array(Vec<Json>),
    /// BTreeMap gives deterministic key ordering.
    Object(BTreeMap<String, Json>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(m) => m.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => Some(*i),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn into_object(self) -> Option<BTreeMap<String, Json>> {
        match self {
            Json::Object(m) => Some(m),
            _ => None,
        }
    }

    pub fn pretty(&self) -> String {
        let mut out = String::new();
        write_pretty(self, 0, &mut out);
        out.push('\n');
        out
    }

    pub fn compact(&self) -> String {
        let mut out = String::new();
        write_compact(self, &mut out);
        out
    }
}

pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
    Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

impl std::fmt::Display for Json {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.compact())
    }
}

fn escape(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
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
    out.push('"');
}

fn write_compact(v: &Json, out: &mut String) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Json::Str(s) => escape(s, out),
        Json::Array(a) => {
            out.push('[');
            for (i, v) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_compact(v, out);
            }
            out.push(']');
        }
        Json::Object(m) => {
            out.push('{');
            for (i, (k, v)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape(k, out);
                out.push(':');
                write_compact(v, out);
            }
            out.push('}');
        }
    }
}

fn write_pretty(v: &Json, indent: usize, out: &mut String) {
    let pad = "  ".repeat(indent);
    match v {
        Json::Array(a) if a.is_empty() => out.push_str("[]"),
        Json::Object(m) if m.is_empty() => out.push_str("{}"),
        Json::Array(a) => {
            out.push_str("[\n");
            for (i, v) in a.iter().enumerate() {
                out.push_str(&"  ".repeat(indent + 1));
                write_pretty(v, indent + 1, out);
                if i + 1 < a.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&pad);
            out.push(']');
        }
        Json::Object(m) => {
            out.push_str("{\n");
            for (i, (k, v)) in m.iter().enumerate() {
                out.push_str(&"  ".repeat(indent + 1));
                escape(k, out);
                out.push_str(": ");
                write_pretty(v, indent + 1, out);
                if i + 1 < m.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&pad);
            out.push('}');
        }
        other => write_compact(other, out),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct ParseError {
    pub msg: String,
    pub pos: usize,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "json parse error at byte {}: {}", self.pos, self.msg)
    }
}

impl std::error::Error for ParseError {}

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
}

pub fn parse(input: &str) -> Result<Json, ParseError> {
    let mut p = Parser {
        b: input.as_bytes(),
        pos: 0,
    };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.pos != p.b.len() {
        return Err(p.err("trailing characters"));
    }
    Ok(v)
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> ParseError {
        ParseError {
            msg: msg.to_string(),
            pos: self.pos,
        }
    }

    fn ws(&mut self) {
        while self.pos < self.b.len() && matches!(self.b[self.pos], b' ' | b'\t' | b'\n' | b'\r') {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.pos).copied()
    }

    fn expect(&mut self, c: u8) -> Result<(), ParseError> {
        if self.peek() == Some(c) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", c as char)))
        }
    }

    fn value(&mut self) -> Result<Json, ParseError> {
        self.ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') | Some(b'f') => self.boolean(),
            Some(b'n') => self.null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(self.err("unexpected token")),
        }
    }

    fn object(&mut self) -> Result<Json, ParseError> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(map));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("expected string key"));
            }
            let key = self.string()?;
            self.ws();
            self.expect(b':')?;
            let v = self.value()?;
            map.insert(key, v);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
        Ok(Json::Object(map))
    }

    fn array(&mut self) -> Result<Json, ParseError> {
        self.expect(b'[')?;
        let mut list = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(list));
        }
        loop {
            list.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
        Ok(Json::Array(list))
    }

    fn string(&mut self) -> Result<String, ParseError> {
        self.expect(b'"')?;
        let mut raw: Vec<u8> = Vec::new();
        loop {
            if self.pos >= self.b.len() {
                return Err(self.err("unterminated string"));
            }
            let c = self.b[self.pos];
            self.pos += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self
                        .b
                        .get(self.pos)
                        .copied()
                        .ok_or(self.err("bad escape"))?;
                    self.pos += 1;
                    match e {
                        b'"' => raw.push(b'"'),
                        b'\\' => raw.push(b'\\'),
                        b'/' => raw.push(b'/'),
                        b'n' => raw.push(b'\n'),
                        b'r' => raw.push(b'\r'),
                        b't' => raw.push(b'\t'),
                        b'b' => raw.push(0x08),
                        b'f' => raw.push(0x0c),
                        b'u' => {
                            if self.pos + 4 > self.b.len() {
                                return Err(self.err("bad \\u escape"));
                            }
                            let hex = std::str::from_utf8(&self.b[self.pos..self.pos + 4])
                                .map_err(|_| self.err("bad \\u escape"))?;
                            let code = u32::from_str_radix(hex, 16)
                                .map_err(|_| self.err("bad \\u escape"))?;
                            self.pos += 4;
                            let cp = if (0xD800..=0xDBFF).contains(&code) {
                                // High surrogate; require low surrogate.
                                if self.b.get(self.pos..self.pos + 2) != Some(b"\\u") {
                                    return Err(self.err("expected low surrogate"));
                                }
                                self.pos += 2;
                                if self.pos + 4 > self.b.len() {
                                    return Err(self.err("bad surrogate pair"));
                                }
                                let hex2 = std::str::from_utf8(&self.b[self.pos..self.pos + 4])
                                    .map_err(|_| self.err("bad surrogate pair"))?;
                                let low = u32::from_str_radix(hex2, 16)
                                    .map_err(|_| self.err("bad surrogate pair"))?;
                                self.pos += 4;
                                if !(0xDC00..=0xDFFF).contains(&low) {
                                    return Err(self.err("bad low surrogate"));
                                }
                                0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            } else {
                                code
                            };
                            let ch = char::from_u32(cp).ok_or_else(|| self.err("bad codepoint"))?;
                            let mut buf = [0u8; 4];
                            raw.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return Err(self.err("bad escape")),
                    }
                }
                _ => {
                    // Raw UTF-8 byte sequences are copied verbatim;
                    // bare control bytes (outside escapes) are invalid JSON.
                    if c < 0x20 {
                        return Err(self.err("unescaped control character"));
                    }
                    raw.push(c);
                }
            }
        }
        String::from_utf8(raw).map_err(|_| self.err("invalid utf-8 in string"))
    }

    fn boolean(&mut self) -> Result<Json, ParseError> {
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

    fn null(&mut self) -> Result<Json, ParseError> {
        if self.b[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(self.err("invalid literal"))
        }
    }

    fn number(&mut self) -> Result<Json, ParseError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let digit_start = self.pos;
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }
        if self.pos == digit_start {
            return Err(self.err("invalid number"));
        }
        // Standard JSON forbids leading zeros (e.g. "01").
        if self.b[digit_start] == b'0' && self.pos > digit_start + 1 {
            return Err(self.err("leading zeros are not allowed"));
        }
        // Reject fractions/exponents: this project only uses integer JSON numbers.
        if matches!(self.peek(), Some(b'.') | Some(b'e') | Some(b'E')) {
            return Err(self.err("only integer numbers are supported"));
        }
        let text =
            std::str::from_utf8(&self.b[start..self.pos]).map_err(|_| self.err("bad number"))?;
        text.parse::<i64>()
            .map(Json::Int)
            .map_err(|_| self.err("integer out of range"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_basic() {
        let input = r#"{"a":[1,2,-3],"b":"x\ny","c":true,"d":null}"#;
        let v = parse(input).unwrap();
        assert_eq!(v.get("a").unwrap().as_array().unwrap().len(), 3);
        assert_eq!(
            v.get("a").unwrap().as_array().unwrap()[2].as_i64(),
            Some(-3)
        );
        assert_eq!(v.get("b").unwrap().as_str(), Some("x\ny"));
        assert_eq!(v.get("c").unwrap().as_bool(), Some(true));
        let again = parse(&v.compact()).unwrap();
        assert_eq!(v, again);
    }

    #[test]
    fn rejects_bad_json() {
        assert!(parse("").is_err());
        assert!(parse("{").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse(r#"{"a":}"#).is_err());
        assert!(parse("1.5").is_err());
        assert!(parse("01").is_err());
        assert!(parse(r#""bad "#).is_err());
        assert!(parse("null x").is_err());
    }

    #[test]
    fn unicode_escape() {
        let v = parse(r#""é é 😀""#).unwrap();
        assert_eq!(v.as_str(), Some("é é 😀"));
    }
}
