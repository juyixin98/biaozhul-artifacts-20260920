//! Minimal JSON support for the control entry: a flat-object parser
//! (string / non-negative integer / boolean / null values) and string
//! escaping for responses. Kept dependency-free on purpose; nested objects
//! and arrays are rejected with a clear error.

use crate::error::{Error, Result};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JsonValue {
    Str(String),
    Num(u64),
    Bool(bool),
    Null,
}

/// Parse a flat JSON object into a key/value map.
pub fn parse_object(input: &str) -> Result<BTreeMap<String, JsonValue>> {
    let mut p = Parser {
        b: input.as_bytes(),
        i: 0,
    };
    p.skip_ws();
    p.expect(b'{')?;
    let mut map = BTreeMap::new();
    p.skip_ws();
    if p.peek() == Some(b'}') {
        p.i += 1;
        p.skip_ws();
        p.expect_end()?;
        return Ok(map);
    }
    loop {
        p.skip_ws();
        let key = p.parse_string()?;
        p.skip_ws();
        p.expect(b':')?;
        p.skip_ws();
        let value = p.parse_value()?;
        map.insert(key, value);
        p.skip_ws();
        match p.peek() {
            Some(b',') => p.i += 1,
            Some(b'}') => {
                p.i += 1;
                break;
            }
            _ => return Err(p.err("expected ',' or '}'")),
        }
    }
    p.skip_ws();
    p.expect_end()?;
    Ok(map)
}

/// Escape a string for inclusion in a JSON response.
pub fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> Error {
        Error::Json(format!("{msg} at byte {}", self.i))
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn expect(&mut self, c: u8) -> Result<()> {
        if self.peek() == Some(c) {
            self.i += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", c as char)))
        }
    }

    fn expect_end(&self) -> Result<()> {
        if self.i == self.b.len() {
            Ok(())
        } else {
            Err(self.err("trailing characters after object"))
        }
    }

    fn parse_string(&mut self) -> Result<String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err(self.err("unterminated string")),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.i += 1;
                    let e = self.peek().ok_or_else(|| self.err("bad escape"))?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'n' => out.push('\n'),
                        b't' => out.push('\t'),
                        b'r' => out.push('\r'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000C}'),
                        b'u' => {
                            let cp = self.parse_hex4()?;
                            let ch = char::from_u32(cp)
                                .ok_or_else(|| self.err("invalid \\u escape"))?;
                            out.push(ch);
                        }
                        _ => return Err(self.err("unknown escape")),
                    }
                }
                Some(c) if c < 0x80 => {
                    out.push(c as char);
                    self.i += 1;
                }
                Some(_) => {
                    // Multi-byte UTF-8: copy the full sequence.
                    let s = std::str::from_utf8(&self.b[self.i..])
                        .map_err(|_| self.err("invalid utf-8"))?;
                    let ch = s.chars().next().ok_or_else(|| self.err("invalid utf-8"))?;
                    out.push(ch);
                    self.i += ch.len_utf8();
                }
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u32> {
        if self.i + 4 > self.b.len() {
            return Err(self.err("truncated \\u escape"));
        }
        let hex = std::str::from_utf8(&self.b[self.i..self.i + 4])
            .map_err(|_| self.err("invalid \\u escape"))?;
        let cp = u32::from_str_radix(hex, 16).map_err(|_| self.err("invalid \\u escape"))?;
        self.i += 4;
        Ok(cp)
    }

    fn parse_value(&mut self) -> Result<JsonValue> {
        match self.peek() {
            Some(b'"') => Ok(JsonValue::Str(self.parse_string()?)),
            Some(b'0'..=b'9') => self.parse_number(),
            Some(b't') => self.parse_lit("true", JsonValue::Bool(true)),
            Some(b'f') => self.parse_lit("false", JsonValue::Bool(false)),
            Some(b'n') => self.parse_lit("null", JsonValue::Null),
            Some(b'{' | b'[') => Err(self.err("nested objects/arrays are not supported")),
            _ => Err(self.err("expected a value")),
        }
    }

    fn parse_lit(&mut self, lit: &str, v: JsonValue) -> Result<JsonValue> {
        if self.b[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            Ok(v)
        } else {
            Err(self.err(&format!("expected '{lit}'")))
        }
    }

    fn parse_number(&mut self) -> Result<JsonValue> {
        let start = self.i;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.i += 1;
        }
        if matches!(self.peek(), Some(b'.' | b'e' | b'E' | b'-' | b'+')) {
            return Err(self.err("only non-negative integers are supported"));
        }
        let text = std::str::from_utf8(&self.b[start..self.i])
            .map_err(|_| self.err("invalid number"))?;
        let n: u64 = text.parse().map_err(|_| self.err("number out of range"))?;
        Ok(JsonValue::Num(n))
    }
}
