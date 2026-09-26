//! Minimal JSON parser/serializer, sufficient for the control interface.
//! Self-contained (no external crates). Supports objects, arrays, strings,
//! numbers (u64/f64), booleans and null.

use std::collections::BTreeMap;
use std::fmt::Write as _;

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
            Json::Num(n) if *n >= 0.0 && n.fract() == 0.0 && *n <= u64::MAX as f64 => {
                Some(*n as u64)
            }
            _ => None,
        }
    }
}

/// Parse a JSON document. Trailing garbage is an error.
pub fn parse(input: &str) -> Result<Json, String> {
    let mut p = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(format!("trailing data at byte {}", p.pos));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, c: u8) -> Result<(), String> {
        if self.peek() == Some(c) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected '{}' at byte {}", c as char, self.pos))
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => self.keyword("true", Json::Bool(true)),
            Some(b'f') => self.keyword("false", Json::Bool(false)),
            Some(b'n') => self.keyword("null", Json::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(c) => Err(format!("unexpected byte 0x{c:02x} at {}", self.pos)),
            None => Err("unexpected end of input".into()),
        }
    }

    fn keyword(&mut self, word: &str, value: Json) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(format!("invalid literal at byte {}", self.pos))
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        self.expect(b'{')?;
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Obj(map));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            let val = self.value()?;
            map.insert(key, val);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Json::Obj(map));
                }
                _ => return Err(format!("expected ',' or '}}' at byte {}", self.pos)),
            }
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Arr(items));
        }
        loop {
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.pos)),
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut s = String::new();
        loop {
            match self.peek() {
                None => return Err("unterminated string".into()),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(s);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    self.escape(&mut s)?;
                }
                Some(c) if c < 0x20 => {
                    return Err(format!("control byte in string at {}", self.pos));
                }
                Some(_) => {
                    // Copy one UTF-8 code point verbatim.
                    let start = self.pos;
                    self.pos += 1;
                    while self.peek().is_some_and(|c| c & 0xC0 == 0x80) {
                        self.pos += 1;
                    }
                    s.push_str(
                        std::str::from_utf8(&self.bytes[start..self.pos])
                            .map_err(|_| "invalid UTF-8 in string".to_string())?,
                    );
                }
            }
        }
    }

    fn escape(&mut self, s: &mut String) -> Result<(), String> {
        let c = self.peek().ok_or("unterminated escape")?;
        self.pos += 1;
        match c {
            b'"' => s.push('"'),
            b'\\' => s.push('\\'),
            b'/' => s.push('/'),
            b'b' => s.push('\u{0008}'),
            b'f' => s.push('\u{000C}'),
            b'n' => s.push('\n'),
            b'r' => s.push('\r'),
            b't' => s.push('\t'),
            b'u' => {
                let cp = self.hex4()?;
                if (0xD800..0xDC00).contains(&cp) {
                    // High surrogate: require a low surrogate pair.
                    if self.peek() == Some(b'\\') {
                        self.pos += 1;
                        self.expect(b'u')?;
                        let lo = self.hex4()?;
                        if !(0xDC00..0xE000).contains(&lo) {
                            return Err("invalid low surrogate".into());
                        }
                        let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        s.push(char::from_u32(c).ok_or("invalid code point")?);
                    } else {
                        return Err("lone high surrogate".into());
                    }
                } else if (0xDC00..0xE000).contains(&cp) {
                    return Err("lone low surrogate".into());
                } else {
                    s.push(char::from_u32(cp).ok_or("invalid code point")?);
                }
            }
            _ => return Err(format!("invalid escape '\\{}'", c as char)),
        }
        Ok(())
    }

    fn hex4(&mut self) -> Result<u32, String> {
        if self.pos + 4 > self.bytes.len() {
            return Err("truncated \\u escape".into());
        }
        let hex = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
            .map_err(|_| "invalid \\u escape".to_string())?;
        let v = u32::from_str_radix(hex, 16).map_err(|_| "invalid \\u escape".to_string())?;
        self.pos += 4;
        Ok(v)
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while matches!(
            self.peek(),
            Some(b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-')
        ) {
            self.pos += 1;
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| "invalid number".to_string())?;
        text.parse::<f64>()
            .map(Json::Num)
            .map_err(|_| format!("invalid number '{text}'"))
    }
}

/// Escape a string for inclusion in JSON output.
pub fn escape_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_flat_object() {
        let v = parse(r#"{"op":"encode","data":"Zm9v","max_output":1024}"#).unwrap();
        assert_eq!(v.get("op").and_then(Json::as_str), Some("encode"));
        assert_eq!(v.get("data").and_then(Json::as_str), Some("Zm9v"));
        assert_eq!(v.get("max_output").and_then(Json::as_u64), Some(1024));
    }

    #[test]
    fn parses_escapes_and_unicode() {
        let v = parse(r#"{"s":"a\nbé😀"}"#).unwrap();
        assert_eq!(v.get("s").and_then(Json::as_str), Some("a\nbé😀"));
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse("").is_err());
        assert!(parse("{").is_err());
        assert!(parse(r#"{"a":1} x"#).is_err());
        assert!(parse(r#"{"a":"\q"}"#).is_err());
        assert!(parse("[1,]").is_err());
    }

    #[test]
    fn escapes_strings() {
        assert_eq!(escape_str("a\"b\\c\nd"), "a\\\"b\\\\c\\nd");
    }
}
