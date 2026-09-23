//! Minimal JSON value, parser and serializer — just enough for this
//! project's HTTP requests and responses. Not a general-purpose JSON
//! library (numbers are f64 on parse; the server emits integers itself).

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

impl Json {
    pub fn obj() -> Json {
        Json::Obj(BTreeMap::new())
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(m) => m.get(key),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Num(n) if n.fract() == 0.0 => Some(*n as u64),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Arr(a) => Some(a),
            _ => None,
        }
    }

    pub fn insert(&mut self, key: &str, val: Json) {
        if let Json::Obj(m) = self {
            m.insert(key.to_string(), val);
        }
    }

    pub fn string(val: impl Into<String>) -> Json {
        Json::Str(val.into())
    }

    pub fn uint(val: u64) -> Json {
        Json::Num(val as f64)
    }

    pub fn parse(input: &str) -> Result<Json, String> {
        let bytes = input.as_bytes();
        let mut pos = 0;
        let v = Parser {
            bytes,
            pos: &mut pos,
        }
        .parse_value()?;
        skip_ws(bytes, &mut pos);
        if pos != bytes.len() {
            return Err(format!("trailing data at byte {pos}"));
        }
        Ok(v)
    }

    /// Serialize compactly.
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut Vec<u8>) {
        match self {
            Json::Null => out.extend_from_slice(b"null"),
            Json::Bool(b) => out.extend_from_slice(if *b { b"true" } else { b"false" }),
            Json::Num(n) => {
                if n.fract() == 0.0 && n.is_finite() {
                    out.extend_from_slice(format!("{}", *n as i64).as_bytes());
                } else {
                    out.extend_from_slice(n.to_string().as_bytes());
                }
            }
            Json::Str(s) => write_json_string(s, out),
            Json::Arr(a) => {
                out.push(b'[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(b',');
                    }
                    v.write(out);
                }
                out.push(b']');
            }
            Json::Obj(m) => {
                out.push(b'{');
                for (i, (k, v)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push(b',');
                    }
                    write_json_string(k, out);
                    out.push(b':');
                    v.write(out);
                }
                out.push(b'}');
            }
        }
    }
}

fn write_json_string(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for c in s.chars() {
        match c {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            c if (c as u32) < 0x20 => {
                out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes())
            }
            c => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: &'a mut usize,
}

fn skip_ws(b: &[u8], pos: &mut usize) {
    while *pos < b.len() && matches!(b[*pos], b' ' | b'\t' | b'\n' | b'\r') {
        *pos += 1;
    }
}

impl<'a> Parser<'a> {
    fn parse_value(&mut self) -> Result<Json, String> {
        skip_ws(self.bytes, self.pos);
        if *self.pos >= self.bytes.len() {
            return Err("unexpected end of input".into());
        }
        match self.bytes[*self.pos] {
            b'{' => self.parse_object(),
            b'[' => self.parse_array(),
            b'"' => Ok(Json::Str(self.parse_string()?)),
            b't' | b'f' => self.parse_bool(),
            b'n' => self.parse_null(),
            b'-' | b'0'..=b'9' => self.parse_number(),
            c => Err(format!("unexpected byte {c} at {}", self.pos)),
        }
    }

    fn parse_object(&mut self) -> Result<Json, String> {
        *self.pos += 1; // {
        let mut m = BTreeMap::new();
        skip_ws(self.bytes, self.pos);
        if self.peek() == Some(b'}') {
            *self.pos += 1;
            return Ok(Json::Obj(m));
        }
        loop {
            skip_ws(self.bytes, self.pos);
            let key = self.parse_string()?;
            skip_ws(self.bytes, self.pos);
            self.expect(b':')?;
            let val = self.parse_value()?;
            m.insert(key, val);
            skip_ws(self.bytes, self.pos);
            match self.next_byte() {
                Some(b',') => continue,
                Some(b'}') => break,
                other => return Err(format!("expected , or }} got {other:?}")),
            }
        }
        Ok(Json::Obj(m))
    }

    fn parse_array(&mut self) -> Result<Json, String> {
        *self.pos += 1; // [
        let mut a = Vec::new();
        skip_ws(self.bytes, self.pos);
        if self.peek() == Some(b']') {
            *self.pos += 1;
            return Ok(Json::Arr(a));
        }
        loop {
            a.push(self.parse_value()?);
            skip_ws(self.bytes, self.pos);
            match self.next_byte() {
                Some(b',') => continue,
                Some(b']') => break,
                other => return Err(format!("expected , or ] got {other:?}")),
            }
        }
        Ok(Json::Arr(a))
    }

    fn parse_string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            match self.next_byte() {
                None => return Err("unterminated string".into()),
                Some(b'"') => break,
                Some(b'\\') => match self.next_byte() {
                    Some(b'"') => out.push('"'),
                    Some(b'\\') => out.push('\\'),
                    Some(b'/') => out.push('/'),
                    Some(b'n') => out.push('\n'),
                    Some(b'r') => out.push('\r'),
                    Some(b't') => out.push('\t'),
                    Some(b'b') => out.push('\u{0008}'),
                    Some(b'f') => out.push('\u{000C}'),
                    Some(b'u') => {
                        let cp = self.parse_hex4()?;
                        if (0xD800..=0xDBFF).contains(&cp) {
                            if self.next_byte() != Some(b'\\') || self.next_byte() != Some(b'u') {
                                return Err("bad surrogate pair".into());
                            }
                            let lo = self.parse_hex4()?;
                            let c = 0x10000 + (((cp - 0xD800) << 10) | (lo - 0xDC00));
                            out.push(char::from_u32(c).ok_or("bad codepoint")?);
                        } else {
                            out.push(char::from_u32(cp).ok_or("bad codepoint")?);
                        }
                    }
                    other => return Err(format!("bad escape {other:?}")),
                },
                Some(b) => {
                    // UTF-8 pass-through: collect the whole char.
                    let width = utf8_width(b);
                    let mut buf = vec![b];
                    for _ in 1..width {
                        buf.push(self.next_byte().ok_or("bad utf-8 in string")?);
                    }
                    out.push_str(std::str::from_utf8(&buf).map_err(|e| e.to_string())?);
                }
            }
        }
        Ok(out)
    }

    fn parse_hex4(&mut self) -> Result<u32, String> {
        let mut v = 0u32;
        for _ in 0..4 {
            let b = self.next_byte().ok_or("short \\u escape")?;
            let d = match b {
                b'0'..=b'9' => b - b'0',
                b'a'..=b'f' => b - b'a' + 10,
                b'A'..=b'F' => b - b'A' + 10,
                _ => return Err("bad hex digit".into()),
            };
            v = v * 16 + d as u32;
        }
        Ok(v)
    }

    fn parse_bool(&mut self) -> Result<Json, String> {
        if self.bytes[*self.pos..].starts_with(b"true") {
            *self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.bytes[*self.pos..].starts_with(b"false") {
            *self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err("bad literal".into())
        }
    }

    fn parse_null(&mut self) -> Result<Json, String> {
        if self.bytes[*self.pos..].starts_with(b"null") {
            *self.pos += 4;
            Ok(Json::Null)
        } else {
            Err("bad literal".into())
        }
    }

    fn parse_number(&mut self) -> Result<Json, String> {
        let start = *self.pos;
        if self.peek() == Some(b'-') {
            *self.pos += 1;
        }
        while let Some(b) = self.peek() {
            if matches!(b, b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-') {
                *self.pos += 1;
            } else {
                break;
            }
        }
        let s = std::str::from_utf8(&self.bytes[start..*self.pos]).map_err(|e| e.to_string())?;
        s.parse::<f64>().map(Json::Num).map_err(|e| e.to_string())
    }

    fn expect(&mut self, b: u8) -> Result<(), String> {
        if self.next_byte() == Some(b) {
            Ok(())
        } else {
            Err(format!("expected byte {b} at {}", *self.pos - 1))
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(*self.pos).copied()
    }

    fn next_byte(&mut self) -> Option<u8> {
        let b = self.bytes.get(*self.pos).copied();
        if b.is_some() {
            *self.pos += 1;
        }
        b
    }
}

fn utf8_width(first: u8) -> usize {
    if first < 0x80 {
        1
    } else if first >> 5 == 0b110 {
        2
    } else if first >> 4 == 0b1110 {
        3
    } else if first >> 3 == 0b11110 {
        4
    } else {
        1
    }
}
