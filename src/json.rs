//! A small, strict, dependency-free JSON parser and serializer.
//!
//! Only the subset needed by the IFIX control protocol is supported, but the
//! parser itself is general: objects, arrays, strings (with full escape and
//! surrogate-pair handling), integers, floats, booleans and null. It rejects
//! comments, trailing commas, trailing content and malformed escapes.

use std::collections::BTreeMap;
use std::fmt::Write as _;

/// A JSON value.
#[derive(Debug, Clone)]
pub enum Value {
    /// `null`.
    Null,
    /// Boolean.
    Bool(bool),
    /// Integer literal (no `.`/exponent).
    Int(i64),
    /// Floating-point literal.
    Float(f64),
    /// UTF-8 string.
    Str(String),
    /// Array preserving document order.
    Array(Vec<Value>),
    /// Object preserving first-insertion order.
    Object(Vec<(String, Value)>),
}

impl Value {
    /// Object field by name.
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(entries) => entries.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// String field.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Integer field (also accepts exact float-valued integers).
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Float(f) if f.fract() == 0.0 && f.is_finite() => Some(*f as i64),
            _ => None,
        }
    }

    /// Boolean field.
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// Array field.
    pub fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Array(a) => Some(a),
            _ => None,
        }
    }

    /// Object entries.
    pub fn as_object(&self) -> Option<&Vec<(String, Value)>> {
        match self {
            Value::Object(o) => Some(o),
            _ => None,
        }
    }
}

/// Parse a JSON document, requiring the whole input to be consumed.
pub fn parse(input: &str) -> Result<Value, String> {
    let bytes = input.as_bytes();
    let mut p = Parser { b: bytes, i: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(format!("trailing data at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn value(&mut self) -> Result<Value, String> {
        self.ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') | Some(b'f') => self.boolean(),
            Some(b'n') => self.null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(c) => Err(format!("unexpected byte {c:#x} at {}", self.i)),
            None => Err("unexpected end of input".to_string()),
        }
    }

    fn object(&mut self) -> Result<Value, String> {
        self.i += 1; // {
        let mut entries = Vec::new();
        let mut seen = BTreeMap::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Value::Object(entries));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(format!("expected string key at byte {}", self.i));
            }
            let key = self.string()?;
            if seen.insert(key.clone(), ()).is_some() {
                return Err(format!("duplicate key {key:?}"));
            }
            self.ws();
            if self.peek() != Some(b':') {
                return Err(format!("expected ':' at byte {}", self.i));
            }
            self.i += 1;
            let v = self.value()?;
            entries.push((key, v));
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                }
                Some(b'}') => {
                    self.i += 1;
                    return Ok(Value::Object(entries));
                }
                _ => return Err(format!("expected ',' or '}}' at byte {}", self.i)),
            }
        }
    }

    fn array(&mut self) -> Result<Value, String> {
        self.i += 1; // [
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                }
                Some(b']') => {
                    self.i += 1;
                    return Ok(Value::Array(items));
                }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.i)),
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            if self.i >= self.b.len() {
                return Err("unterminated string".to_string());
            }
            let c = self.b[self.i];
            self.i += 1;
            match c {
                b'"' => return Ok(out),
                b'\\' => {
                    let e = *self.b.get(self.i).ok_or("dangling escape")?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let cp = self.hex4()?;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                // High surrogate; need \uXXXX low surrogate.
                                if self.b.get(self.i..self.i + 2) != Some(b"\\u") {
                                    return Err("bad surrogate pair".to_string());
                                }
                                self.i += 2;
                                let lo = self.hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err("bad low surrogate".to_string());
                                }
                                let c =
                                    0x10000 + (((cp - 0xD800) as u32) << 10) + (lo - 0xDC00) as u32;
                                out.push(char::from_u32(c).ok_or("bad surrogate codepoint")?);
                            } else if (0xDC00..=0xDFFF).contains(&cp) {
                                return Err("unexpected low surrogate".to_string());
                            } else {
                                out.push(char::from_u32(cp as u32).ok_or("bad \\u codepoint")?);
                            }
                        }
                        _ => return Err(format!("bad escape \\{e}")),
                    }
                }
                _ => {
                    // Copy a run of UTF-8 bytes, validating at the boundary.
                    let start = self.i - 1;
                    while self.i < self.b.len()
                        && !matches!(self.b[self.i], b'"' | b'\\')
                        && self.b[self.i] >= 0x20
                    {
                        self.i += 1;
                    }
                    let chunk = std::str::from_utf8(&self.b[start..self.i])
                        .map_err(|_| "invalid utf-8 in string".to_string())?;
                    out.push_str(chunk);
                    if self.i < self.b.len() && self.b[self.i] < 0x20 {
                        return Err("unescaped control character".to_string());
                    }
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u16, String> {
        if self.i + 4 > self.b.len() {
            return Err("short \\u escape".to_string());
        }
        let mut v = 0u16;
        for _ in 0..4 {
            let c = self.b[self.i];
            self.i += 1;
            let d = (c as char).to_digit(16).ok_or("bad hex digit")?;
            v = v * 16 + d as u16;
        }
        Ok(v)
    }

    fn boolean(&mut self) -> Result<Value, String> {
        if self.b[self.i..].starts_with(b"true") {
            self.i += 4;
            Ok(Value::Bool(true))
        } else if self.b[self.i..].starts_with(b"false") {
            self.i += 5;
            Ok(Value::Bool(false))
        } else {
            Err("bad literal".to_string())
        }
    }

    fn null(&mut self) -> Result<Value, String> {
        if self.b[self.i..].starts_with(b"null") {
            self.i += 4;
            Ok(Value::Null)
        } else {
            Err("bad literal".to_string())
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        let mut is_float = false;
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(c) if c.is_ascii_digit() => {
                while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                    self.i += 1;
                }
            }
            _ => return Err("bad number".to_string()),
        }
        if self.peek() == Some(b'.') {
            is_float = true;
            self.i += 1;
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return Err("bad fraction".to_string());
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            is_float = true;
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return Err("bad exponent".to_string());
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).map_err(|_| "bad number")?;
        if is_float {
            Ok(Value::Float(text.parse().map_err(|_| "bad float")?))
        } else {
            Ok(Value::Int(
                text.parse().map_err(|_| "integer out of range")?,
            ))
        }
    }
}

/// Serialize compactly.
pub fn to_string(v: &Value) -> String {
    let mut out = String::new();
    write_into(&mut out, v, 0, false);
    out
}

/// Serialize with two-space indentation.
pub fn to_string_pretty(v: &Value) -> String {
    let mut out = String::new();
    write_into(&mut out, v, 0, true);
    out
}

fn write_into(out: &mut String, v: &Value, depth: usize, pretty: bool) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Value::Float(f) => {
            if f.is_finite() {
                let _ = write!(out, "{f}");
            } else {
                out.push_str("null");
            }
        }
        Value::Str(s) => write_json_string(out, s),
        Value::Array(a) => {
            if a.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                if pretty {
                    out.push('\n');
                    out.push_str(&"  ".repeat(depth + 1));
                }
                write_into(out, item, depth + 1, pretty);
            }
            if pretty {
                out.push('\n');
                out.push_str(&"  ".repeat(depth));
            }
            out.push(']');
        }
        Value::Object(o) => {
            if o.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, val)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                if pretty {
                    out.push('\n');
                    out.push_str(&"  ".repeat(depth + 1));
                }
                write_json_string(out, k);
                out.push(':');
                if pretty {
                    out.push(' ');
                }
                write_into(out, val, depth + 1, pretty);
            }
            if pretty {
                out.push('\n');
                out.push_str(&"  ".repeat(depth));
            }
            out.push('}');
        }
    }
}

fn write_json_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_structure() {
        let doc = r#"{"op":"build","tree":{"name":"","type":"dir","children":[
          {"name":"a","type":"file","mode":420,"content_base64":"Zm9v"}
        ]}}"#;
        let v = parse(doc).unwrap();
        assert_eq!(v.get("op").unwrap().as_str(), Some("build"));
        let child = &v
            .get("tree")
            .unwrap()
            .get("children")
            .unwrap()
            .as_array()
            .unwrap()[0];
        assert_eq!(child.get("mode").unwrap().as_i64(), Some(420));
        let again = parse(&to_string(&v)).unwrap();
        assert_eq!(again.get("op").unwrap().as_str(), Some("build"));
    }

    #[test]
    fn escapes_and_surrogates() {
        let v = parse("\"a\\n\\u00e9\\ud83d\\ude00\"").unwrap();
        assert_eq!(v.as_str(), Some("a\né😀"));
    }

    #[test]
    fn rejects_bad_inputs() {
        assert!(parse("").is_err());
        assert!(parse("01").is_err());
        assert!(parse("{\"a\":1,}").is_err());
        assert!(parse("[1 2]").is_err());
        assert!(parse("\"bad\\u12\"").is_err());
        assert!(parse("null junk").is_err());
        assert!(parse("{\"a\":1,\"a\":2}").is_err());
    }
}
