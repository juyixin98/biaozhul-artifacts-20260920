//! Minimal self-contained JSON (RFC 8259) parser and serializer.
//!
//! Just enough for the control protocol: objects, arrays, strings
//! (including `\uXXXX` escapes with surrogate pairs), numbers, booleans
//! and null. Numbers are kept as `f64`; integral values up to 2^53 are
//! exact, which covers every offset and limit in the protocol.

use core::fmt;

/// A parsed JSON value. Object members keep their document order.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    /// Look up an object member by key.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(members) => members.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// Non-negative integral value, or None for non-numbers, negatives
    /// and fractions.
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Num(n) if *n >= 0.0 && n.fract() == 0.0 && *n <= u64::MAX as f64 => {
                Some(*n as u64)
            }
            _ => None,
        }
    }

    pub fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(items) => Some(items),
            _ => None,
        }
    }
}

/// A JSON syntax error, with a character offset into the input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub offset: usize,
    pub message: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "JSON error at character {}: {}", self.offset, self.message)
    }
}

impl std::error::Error for ParseError {}

/// Parse a complete JSON document. Trailing content is an error.
pub fn parse(input: &str) -> Result<Json, ParseError> {
    let chars: Vec<char> = input.chars().collect();
    let mut p = Parser { chars: &chars, pos: 0 };
    p.skip_ws();
    let value = p.parse_value()?;
    p.skip_ws();
    if p.pos != chars.len() {
        return Err(p.err("trailing content after JSON value"));
    }
    Ok(value)
}

struct Parser<'a> {
    chars: &'a [char],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, message: &str) -> ParseError {
        ParseError { offset: self.pos, message: message.to_string() }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn next(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.pos += 1;
        Some(c)
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(' ' | '\t' | '\n' | '\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, c: char) -> Result<(), ParseError> {
        if self.peek() == Some(c) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", c)))
        }
    }

    fn parse_value(&mut self) -> Result<Json, ParseError> {
        match self.peek() {
            Some('{') => self.parse_object(),
            Some('[') => self.parse_array(),
            Some('"') => Ok(Json::Str(self.parse_string()?)),
            Some('t') => self.parse_literal("true", Json::Bool(true)),
            Some('f') => self.parse_literal("false", Json::Bool(false)),
            Some('n') => self.parse_literal("null", Json::Null),
            Some(c) if c == '-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => Err(self.err(&format!("unexpected character '{}'", c))),
            None => Err(self.err("unexpected end of input")),
        }
    }

    fn parse_literal(&mut self, word: &str, value: Json) -> Result<Json, ParseError> {
        for expected in word.chars() {
            if self.next() != Some(expected) {
                return Err(self.err(&format!("invalid literal, expected '{}'", word)));
            }
        }
        Ok(value)
    }

    fn parse_object(&mut self) -> Result<Json, ParseError> {
        self.expect('{')?;
        let mut members = Vec::new();
        self.skip_ws();
        if self.peek() == Some('}') {
            self.pos += 1;
            return Ok(Json::Obj(members));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            self.expect(':')?;
            self.skip_ws();
            let value = self.parse_value()?;
            members.push((key, value));
            self.skip_ws();
            match self.next() {
                Some(',') => continue,
                Some('}') => return Ok(Json::Obj(members)),
                _ => return Err(self.err("expected ',' or '}' in object")),
            }
        }
    }

    fn parse_array(&mut self) -> Result<Json, ParseError> {
        self.expect('[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(']') {
            self.pos += 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.parse_value()?);
            self.skip_ws();
            match self.next() {
                Some(',') => continue,
                Some(']') => return Ok(Json::Arr(items)),
                _ => return Err(self.err("expected ',' or ']' in array")),
            }
        }
    }

    fn parse_string(&mut self) -> Result<String, ParseError> {
        self.expect('"')?;
        let mut out = String::new();
        loop {
            match self.next() {
                None => return Err(self.err("unterminated string")),
                Some('"') => return Ok(out),
                Some('\\') => match self.next() {
                    Some('"') => out.push('"'),
                    Some('\\') => out.push('\\'),
                    Some('/') => out.push('/'),
                    Some('b') => out.push('\u{0008}'),
                    Some('f') => out.push('\u{000C}'),
                    Some('n') => out.push('\n'),
                    Some('r') => out.push('\r'),
                    Some('t') => out.push('\t'),
                    Some('u') => {
                        let hi = self.parse_hex4()?;
                        let cp = if (0xD800..0xDC00).contains(&hi) {
                            // High surrogate: a low surrogate must follow.
                            if self.next() != Some('\\') || self.next() != Some('u') {
                                return Err(self.err("unpaired high surrogate in \\u escape"));
                            }
                            let lo = self.parse_hex4()?;
                            if !(0xDC00..0xE000).contains(&lo) {
                                return Err(self.err("expected low surrogate in \\u escape"));
                            }
                            0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                        } else if (0xDC00..0xE000).contains(&hi) {
                            return Err(self.err("unpaired low surrogate in \\u escape"));
                        } else {
                            hi
                        };
                        match char::from_u32(cp) {
                            Some(c) => out.push(c),
                            None => return Err(self.err("invalid \\u escape")),
                        }
                    }
                    _ => return Err(self.err("invalid escape sequence")),
                },
                Some(c) if (c as u32) < 0x20 => {
                    return Err(self.err("unescaped control character in string"))
                }
                Some(c) => out.push(c),
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u32, ParseError> {
        let mut value: u32 = 0;
        for _ in 0..4 {
            let c = self.next().ok_or_else(|| self.err("truncated \\u escape"))?;
            let digit = c.to_digit(16).ok_or_else(|| self.err("invalid hex digit in \\u escape"))?;
            value = value * 16 + digit;
        }
        Ok(value)
    }

    fn parse_number(&mut self) -> Result<Json, ParseError> {
        let start = self.pos;
        if self.peek() == Some('-') {
            self.pos += 1;
        }
        self.consume_digits();
        if self.peek() == Some('.') {
            self.pos += 1;
            self.consume_digits();
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some('+' | '-')) {
                self.pos += 1;
            }
            self.consume_digits();
        }
        let text: String = self.chars[start..self.pos].iter().collect();
        text.parse::<f64>()
            .map(Json::Num)
            .map_err(|_| ParseError { offset: start, message: "invalid number".to_string() })
    }

    fn consume_digits(&mut self) {
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }
    }
}

/// Serialize a JSON value (compact, no whitespace).
pub fn to_string(value: &Json) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Json, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Num(n) => write_number(*n, out),
        Json::Str(s) => write_string(s, out),
        Json::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Json::Obj(members) => {
            out.push('{');
            for (i, (key, val)) in members.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write_value(val, out);
            }
            out.push('}');
        }
    }
}

fn write_number(n: f64, out: &mut String) {
    if n.fract() == 0.0 && n.abs() <= 9_007_199_254_740_992.0 {
        // Integral value within the exact f64 range: print without a
        // decimal point so offsets and limits stay readable.
        out.push_str(&format!("{}", n as i64));
    } else {
        out.push_str(&format!("{}", n));
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
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
    out.push('"');
}

/// Convenience constructors for building responses.
pub fn obj(members: Vec<(&str, Json)>) -> Json {
    Json::Obj(members.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn num(n: u64) -> Json {
    Json::Num(n as f64)
}
