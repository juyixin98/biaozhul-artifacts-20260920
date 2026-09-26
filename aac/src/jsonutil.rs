//! Minimal, dependency-free JSON support for the control entry point.
//!
//! Only the subset needed by the request/response schema is implemented:
//! objects, strings, integers, booleans and null. Arbitrary byte payloads are
//! carried as standard (padded) base64 strings.

use crate::error::{Error, Result};

// ---------------------------------------------------------------------------
// Base64
// ---------------------------------------------------------------------------

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard padded base64 encoding.
pub fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = chunk.get(1).copied().map(u32::from);
        let b2 = chunk.get(2).copied().map(u32::from);
        let triple = (b0 << 16) | (b1.unwrap_or(0) << 8) | b2.unwrap_or(0);
        out.push(B64_ALPHABET[((triple >> 18) & 63) as usize] as char);
        out.push(B64_ALPHABET[((triple >> 12) & 63) as usize] as char);
        match chunk.len() {
            3 => {
                out.push(B64_ALPHABET[((triple >> 6) & 63) as usize] as char);
                out.push(B64_ALPHABET[(triple & 63) as usize] as char);
            }
            2 => {
                out.push(B64_ALPHABET[((triple >> 6) & 63) as usize] as char);
                out.push('=');
            }
            _ => {
                out.push('=');
                out.push('=');
            }
        }
    }
    out
}

fn b64_decode_char(c: u8) -> Option<u8> {
    match c {
        b'A'..=b'Z' => Some(c - b'A'),
        b'a'..=b'z' => Some(c - b'a' + 26),
        b'0'..=b'9' => Some(c - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// Standard padded base64 decoding. Whitespace is rejected.
pub fn base64_decode(input: &str) -> Result<Vec<u8>> {
    let bytes = input.as_bytes();
    if bytes.len() % 4 != 0 {
        return Err(Error::InvalidBase64(
            "length must be a multiple of 4".to_string(),
        ));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let mut vals = [0u8; 4];
        let mut pad = 0;
        for (i, &c) in chunk.iter().enumerate() {
            if c == b'=' {
                vals[i] = 0;
                pad += 1;
            } else {
                if pad > 0 {
                    return Err(Error::InvalidBase64(
                        "padding must only appear at the end".to_string(),
                    ));
                }
                vals[i] = b64_decode_char(c)
                    .ok_or_else(|| Error::InvalidBase64(format!("invalid char {:?}", c as char)))?;
            }
        }
        let triple = (u32::from(vals[0]) << 18)
            | (u32::from(vals[1]) << 12)
            | (u32::from(vals[2]) << 6)
            | u32::from(vals[3]);
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad == 0 {
            out.push(triple as u8);
        }
        if pad == 3 {
            return Err(Error::InvalidBase64("too much padding".to_string()));
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// JSON values
// ---------------------------------------------------------------------------

/// A small JSON value tree.
#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    /// Parsed integers only (the request schema has no floats).
    Int(i64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    /// Object field lookup.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(fields) => fields.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Extract a string.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Extract an integer.
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(n) => Some(*n),
            _ => None,
        }
    }
}

/// Recursive-descent parser over borrowed bytes.
pub fn parse(input: &str) -> Result<Json> {
    let bytes = input.as_bytes();
    let mut p = Parser { b: bytes, pos: 0 };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.b.len() {
        return Err(Error::InvalidJson(format!(
            "trailing data at byte {}",
            p.pos
        )));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
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
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(Json::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            other => Err(Error::InvalidJson(format!(
                "unexpected byte {other:?} at {}",
                self.pos
            ))),
        }
    }

    fn parse_object(&mut self) -> Result<Json> {
        self.pos += 1; // {
        let mut fields = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(fields));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(Error::InvalidJson(format!("expected key at {}", self.pos)));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(Error::InvalidJson(format!("expected ':' at {}", self.pos)));
            }
            self.pos += 1;
            let val = self.parse_value()?;
            fields.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => {
                    return Err(Error::InvalidJson(format!(
                        "expected ',' or '}}' at {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Object(fields))
    }

    fn parse_array(&mut self) -> Result<Json> {
        self.pos += 1; // [
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
                _ => {
                    return Err(Error::InvalidJson(format!(
                        "expected ',' or ']' at {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Array(items))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // opening quote
        let mut s = String::new();
        loop {
            match self.peek() {
                None => return Err(Error::InvalidJson("unterminated string".to_string())),
                Some(b'"') => {
                    self.pos += 1;
                    break;
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let e = self
                        .peek()
                        .ok_or_else(|| Error::InvalidJson("bad escape".to_string()))?;
                    self.pos += 1;
                    match e {
                        b'"' => s.push('"'),
                        b'\\' => s.push('\\'),
                        b'/' => s.push('/'),
                        b'b' => s.push('\u{0008}'),
                        b'f' => s.push('\u{000C}'),
                        b'n' => s.push('\n'),
                        b'r' => s.push('\r'),
                        b't' => s.push('\t'),
                        b'u' => {
                            if self.pos + 4 > self.b.len() {
                                return Err(Error::InvalidJson("bad \\u escape".to_string()));
                            }
                            let hex = std::str::from_utf8(&self.b[self.pos..self.pos + 4])
                                .map_err(|_| Error::InvalidJson("bad \\u escape".to_string()))?;
                            let cp = u32::from_str_radix(hex, 16)
                                .map_err(|_| Error::InvalidJson("bad \\u escape".to_string()))?;
                            self.pos += 4;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                // High surrogate: require \uXXXX low surrogate.
                                if self.b[self.pos..].starts_with(br"\u") {
                                    self.pos += 2;
                                    let hex2 = std::str::from_utf8(&self.b[self.pos..self.pos + 4])
                                        .map_err(|_| {
                                            Error::InvalidJson("bad surrogate".to_string())
                                        })?;
                                    let lo = u32::from_str_radix(hex2, 16).map_err(|_| {
                                        Error::InvalidJson("bad surrogate".to_string())
                                    })?;
                                    self.pos += 4;
                                    if !(0xDC00..=0xDFFF).contains(&lo) {
                                        return Err(Error::InvalidJson(
                                            "bad low surrogate".to_string(),
                                        ));
                                    }
                                    let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                    s.push(char::from_u32(c).ok_or_else(|| {
                                        Error::InvalidJson("bad surrogate pair".to_string())
                                    })?);
                                } else {
                                    return Err(Error::InvalidJson(
                                        "unpaired high surrogate".to_string(),
                                    ));
                                }
                            } else if let Some(c) = char::from_u32(cp) {
                                s.push(c);
                            } else {
                                return Err(Error::InvalidJson(
                                    "bad unicode codepoint".to_string(),
                                ));
                            }
                        }
                        _ => return Err(Error::InvalidJson(format!("bad escape \\{e}"))),
                    }
                }
                Some(_) => {
                    // Copy a run of ordinary UTF-8 bytes.
                    let start = self.pos;
                    self.pos += 1;
                    while let Some(n) = self.peek() {
                        if n == b'"' || n == b'\\' {
                            break;
                        }
                        self.pos += 1;
                    }
                    s.push_str(
                        std::str::from_utf8(&self.b[start..self.pos]).map_err(|_| {
                            Error::InvalidJson("invalid UTF-8 in string".to_string())
                        })?,
                    );
                }
            }
        }
        Ok(s)
    }

    fn parse_bool(&mut self) -> Result<Json> {
        if self.b[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.b[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err(Error::InvalidJson(format!("bad literal at {}", self.pos)))
        }
    }

    fn parse_null(&mut self) -> Result<Json> {
        if self.b[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(Error::InvalidJson(format!("bad literal at {}", self.pos)))
        }
    }

    fn parse_number(&mut self) -> Result<Json> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                self.pos += 1;
            } else {
                break;
            }
        }
        // Reject floats / exponents explicitly: the schema is integer-only.
        if matches!(self.peek(), Some(b'.') | Some(b'e') | Some(b'E')) {
            return Err(Error::InvalidJson(
                "only integer numbers are supported".to_string(),
            ));
        }
        let text = std::str::from_utf8(&self.b[start..self.pos])
            .map_err(|_| Error::InvalidJson("bad number".to_string()))?;
        text.parse::<i64>()
            .map(Json::Int)
            .map_err(|_| Error::InvalidJson(format!("bad integer '{text}'")))
    }
}

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

/// Serialize a string with JSON escaping.
pub fn escape_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
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
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_rfc4648_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");

        for s in ["", "f", "fo", "foo", "foob", "fooba", "foobar"] {
            assert_eq!(
                base64_decode(&base64_encode(s.as_bytes())).unwrap(),
                s.as_bytes()
            );
        }
    }

    #[test]
    fn base64_rejects_garbage() {
        assert!(base64_decode("Zg=").is_err());
        assert!(base64_decode("****").is_err());
        assert!(base64_decode("Z===Zg==").is_err());
    }

    #[test]
    fn json_roundtrip_and_unicode() {
        let v = parse(r#"{"a":"héllo\n","b":-42,"c":true,"d":null,"e":[1,2]}"#).unwrap();
        assert_eq!(v.get("a").unwrap().as_str(), Some("héllo\n"));
        assert_eq!(v.get("b").unwrap().as_i64(), Some(-42));
        assert_eq!(escape_string("a\"b\\c"), "\"a\\\"b\\\\c\"");
    }

    #[test]
    fn json_rejects_bad_inputs() {
        assert!(parse("{").is_err());
        assert!(parse("1.5").is_err());
        assert!(parse("truex").is_err());
        assert!(parse(r#""\ud800""#).is_err());
    }
}
