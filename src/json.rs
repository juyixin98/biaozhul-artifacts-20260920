//! Tiny dependency-free JSON implementation: just enough for the HTTP API
//! (objects, arrays, strings, numbers, booleans, null). Input is trusted to be
//! a syntactically valid JSON object; malformed input yields an error.

use std::collections::BTreeMap;

#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    /// Parsed into i64 when integral, otherwise f64.
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

impl Json {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_object(&self) -> Option<&BTreeMap<String, Json>> {
        match self {
            Json::Obj(o) => Some(o),
            _ => None,
        }
    }
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object().and_then(|o| o.get(key))
    }
    pub fn from_bytes(b: &[u8]) -> Result<Json, String> {
        let s = std::str::from_utf8(b).map_err(|e| e.to_string())?;
        let mut p = Parser {
            chars: s.chars().collect(),
            pos: 0,
        };
        p.skip_ws();
        let v = p.parse_value()?;
        p.skip_ws();
        if p.pos != p.chars.len() {
            return Err(format!("trailing data at {}", p.pos));
        }
        Ok(v)
    }

    pub fn to_string_compact(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => out.push_str(&n.to_string()),
            Json::Float(n) => out.push_str(&n.to_string()),
            Json::Str(s) => write_json_string(s, out),
            Json::Arr(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    v.write(out);
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
                    v.write(out);
                }
                out.push('}');
            }
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
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

struct Parser {
    chars: Vec<char>,
    pos: usize,
}

impl Parser {
    fn skip_ws(&mut self) {
        while self.pos < self.chars.len() && self.chars[self.pos].is_whitespace() {
            self.pos += 1;
        }
    }

    fn parse_value(&mut self) -> Result<Json, String> {
        self.skip_ws();
        if self.pos >= self.chars.len() {
            return Err("unexpected end".into());
        }
        match self.chars[self.pos] {
            '{' => self.parse_obj(),
            '[' => self.parse_arr(),
            '"' => Ok(Json::Str(self.parse_string()?)),
            't' | 'f' => self.parse_bool(),
            'n' => self.parse_null(),
            '-' | '0'..='9' => self.parse_number(),
            c => Err(format!("unexpected char {c} at {}", self.pos)),
        }
    }

    fn parse_obj(&mut self) -> Result<Json, String> {
        self.pos += 1; // {
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some('}') {
            self.pos += 1;
            return Ok(Json::Obj(map));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            if self.next() != Some(':') {
                return Err(format!("expected ':' at {}", self.pos));
            }
            let val = self.parse_value()?;
            map.insert(key, val);
            self.skip_ws();
            match self.next() {
                Some(',') => continue,
                Some('}') => break,
                other => return Err(format!("expected , or }} got {other:?}")),
            }
        }
        Ok(Json::Obj(map))
    }

    fn parse_arr(&mut self) -> Result<Json, String> {
        self.pos += 1; // [
        let mut arr = Vec::new();
        self.skip_ws();
        if self.peek() == Some(']') {
            self.pos += 1;
            return Ok(Json::Arr(arr));
        }
        loop {
            arr.push(self.parse_value()?);
            self.skip_ws();
            match self.next() {
                Some(',') => continue,
                Some(']') => break,
                other => return Err(format!("expected , or ] got {other:?}")),
            }
        }
        Ok(Json::Arr(arr))
    }

    fn parse_string(&mut self) -> Result<String, String> {
        if self.next() != Some('"') {
            return Err(format!("expected string at {}", self.pos));
        }
        let mut s = String::new();
        loop {
            match self.next() {
                None => return Err("unterminated string".into()),
                Some('"') => break,
                Some('\\') => match self.next() {
                    Some('"') => s.push('"'),
                    Some('\\') => s.push('\\'),
                    Some('/') => s.push('/'),
                    Some('n') => s.push('\n'),
                    Some('t') => s.push('\t'),
                    Some('r') => s.push('\r'),
                    Some('b') => s.push('\u{08}'),
                    Some('f') => s.push('\u{0c}'),
                    Some('u') => {
                        let mut code = 0u32;
                        for _ in 0..4 {
                            let c = self.next().ok_or("bad unicode escape")?;
                            code = code * 16
                                + c.to_digit(16).ok_or("bad hex digit")?;
                        }
                        if let Some(ch) = char::from_u32(code) {
                            s.push(ch);
                        }
                    }
                    other => return Err(format!("bad escape {other:?}")),
                },
                Some(c) => s.push(c),
            }
        }
        Ok(s)
    }

    fn parse_bool(&mut self) -> Result<Json, String> {
        if self.chars[self.pos..].starts_with(&['t', 'r', 'u', 'e']) {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.chars[self.pos..].starts_with(&['f', 'a', 'l', 's', 'e']) {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err("bad literal".into())
        }
    }

    fn parse_null(&mut self) -> Result<Json, String> {
        if self.chars[self.pos..].starts_with(&['n', 'u', 'l', 'l']) {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err("bad literal".into())
        }
    }

    fn parse_number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some('-') {
            self.pos += 1;
        }
        let mut is_float = false;
        while let Some(c) = self.peek() {
            match c {
                '0'..='9' => self.pos += 1,
                '.' | 'e' | 'E' | '+' | '-' => {
                    is_float = true;
                    self.pos += 1;
                }
                _ => break,
            }
        }
        let text: String = self.chars[start..self.pos].iter().collect();
        if is_float {
            text.parse::<f64>()
                .map(Json::Float)
                .map_err(|e: std::num::ParseFloatError| e.to_string())
        } else {
            text.parse::<i64>()
                .map(Json::Int)
                .map_err(|e: std::num::ParseIntError| e.to_string())
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }
    fn next(&mut self) -> Option<char> {
        let c = self.chars.get(self.pos).copied();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }
}

/// Convenience constructor.
pub fn obj(pairs: &[(&str, Json)]) -> Json {
    let mut m = BTreeMap::new();
    for (k, v) in pairs {
        m.insert((*k).to_string(), v.clone());
    }
    Json::Obj(m)
}
pub fn s(v: impl Into<String>) -> Json {
    Json::Str(v.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let j = Json::from_bytes(
            br#"{"name":"a b","v":42,"f":1.5,"del":null,"arr":[1,"x",true,-7]}"#,
        )
        .unwrap();
        assert_eq!(j.get("name").and_then(|x| x.as_str()), Some("a b"));
        let text = j.to_string_compact();
        let j2 = Json::from_bytes(text.as_bytes()).unwrap();
        assert_eq!(j2.get("v").and_then(|x| x.as_str()), None);
        if let Json::Int(n) = j2.get("v").unwrap() {
            assert_eq!(*n, 42);
        } else {
            panic!("v not int");
        }
    }

    #[test]
    fn rejects_bad_json() {
        assert!(Json::from_bytes(b"{").is_err());
        assert!(Json::from_bytes(b"{}x").is_err());
        assert!(Json::from_bytes(b"not json").is_err());
    }
}
