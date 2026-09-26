//! A small self-contained JSON parser/serializer and base64 codec.
//!
//! The control entry point accepts JSON requests and returns JSON
//! responses. To keep the library dependency-free (and auditable) this is
//! a hand-written recursive-descent parser rather than an external crate.
//! Integer literals that fit in `i64` parse to [`JsonValue::Int`]; all
//! other numbers parse to [`JsonValue::Float`].

use std::collections::BTreeMap;
use std::fmt::Write as _;

use crate::error::{Error, Result};

/// A JSON value. Objects preserve insertion-independent access; the
/// serializer emits keys in the order they were first inserted via
/// [`JsonObject`].
#[derive(Debug, Clone, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Int(i64),
    /// Unsigned 64-bit literal, used so values above `i64::MAX` (e.g. a
    /// `uint64` field holding `u64::MAX`) keep exact precision instead of
    /// wrapping to a negative signed integer.
    UInt(u64),
    Float(f64),
    Str(String),
    Array(Vec<JsonValue>),
    Object(JsonObject),
}

/// JSON object that remembers key insertion order while offering lookup.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct JsonObject {
    order: Vec<String>,
    map: BTreeMap<String, JsonValue>,
}

impl JsonObject {
    pub fn new() -> JsonObject {
        JsonObject::default()
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    /// Insert or replace a key. Replacement keeps the original position.
    pub fn set(&mut self, key: impl Into<String>, value: JsonValue) {
        let key = key.into();
        if !self.map.contains_key(&key) {
            self.order.push(key.clone());
        }
        self.map.insert(key, value);
    }

    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        self.map.get(key)
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.map.contains_key(key)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &JsonValue)> {
        self.order
            .iter()
            .filter_map(move |k| self.map.get(k).map(|v| (k, v)))
    }
}

impl JsonValue {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&JsonObject> {
        match self {
            JsonValue::Object(o) => Some(o),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<JsonValue>> {
        match self {
            JsonValue::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            JsonValue::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// Signed integer value when the literal fits in `i64`.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            JsonValue::Int(n) => Some(*n),
            JsonValue::UInt(u) => i64::try_from(*u).ok(),
            JsonValue::Float(f) if f.fract() == 0.0 && f.is_finite() => Some(*f as i64),
            _ => None,
        }
    }

    /// Unsigned integer value when the literal is non-negative.
    pub fn as_uint(&self) -> Option<u64> {
        match self {
            JsonValue::UInt(u) => Some(*u),
            JsonValue::Int(n) if *n >= 0 => Some(*n as u64),
            JsonValue::Float(f) if f.fract() == 0.0 && f.is_finite() && *f >= 0.0 => {
                Some(*f as u64)
            }
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

/// Parse a JSON document. Trailing non-whitespace content is an error.
pub fn parse(input: &str) -> Result<JsonValue> {
    let mut p = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let value = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(Error::Json(format!("trailing data at byte {}", p.pos)));
    }
    Ok(value)
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

    fn parse_value(&mut self) -> Result<JsonValue> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(JsonValue::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => Err(Error::Json(format!(
                "unexpected byte '{c}' at {}",
                self.pos
            ))),
            None => Err(Error::Json("unexpected end of input".into())),
        }
    }

    fn parse_object(&mut self) -> Result<JsonValue> {
        self.pos += 1; // {
        let mut obj = JsonObject::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonValue::Object(obj));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(Error::Json(format!("expected string key at {}", self.pos)));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(Error::Json(format!("expected ':' at {}", self.pos)));
            }
            self.pos += 1;
            let value = self.parse_value()?;
            obj.set(key, value);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(Error::Json(format!("expected ',' or '}}' at {}", self.pos))),
            }
        }
        Ok(JsonValue::Object(obj))
    }

    fn parse_array(&mut self) -> Result<JsonValue> {
        self.pos += 1; // [
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(JsonValue::Array(items));
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
                _ => return Err(Error::Json(format!("expected ',' or ']' at {}", self.pos))),
            }
        }
        Ok(JsonValue::Array(items))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            match self.bytes.get(self.pos).copied() {
                None => return Err(Error::Json("unterminated string".into())),
                Some(b'"') => {
                    self.pos += 1;
                    break;
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let esc = self
                        .bytes
                        .get(self.pos)
                        .copied()
                        .ok_or_else(|| Error::Json("unterminated escape".into()))?;
                    self.pos += 1;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000C}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let cp = self.parse_unicode_escape()?;
                            out.push(cp);
                        }
                        other => {
                            return Err(Error::Json(format!("bad escape \\{other}")));
                        }
                    }
                }
                Some(c) if c < 0x80 => {
                    out.push(c as char);
                    self.pos += 1;
                }
                Some(_) => {
                    // Copy a UTF-8 run verbatim after validating it.
                    let start = self.pos;
                    while self.pos < self.bytes.len()
                        && self.bytes[self.pos] >= 0x80
                        && self.bytes[self.pos] != b'"'
                    {
                        self.pos += 1;
                    }
                    let run = std::str::from_utf8(&self.bytes[start..self.pos])
                        .map_err(|_| Error::Json("invalid UTF-8 in string".into()))?;
                    out.push_str(run);
                }
            }
        }
        Ok(out)
    }

    fn parse_unicode_escape(&mut self) -> Result<char> {
        let hi = self.parse_hex4()?;
        if (0xD800..=0xDBFF).contains(&hi) {
            // High surrogate; require a following low surrogate.
            if self.bytes.get(self.pos..self.pos + 2) != Some(b"\\u") {
                return Err(Error::Json("unpaired high surrogate".into()));
            }
            self.pos += 2;
            let lo = self.parse_hex4()?;
            if !(0xDC00..=0xDFFF).contains(&lo) {
                return Err(Error::Json("bad low surrogate".into()));
            }
            let c = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
            char::from_u32(c).ok_or_else(|| Error::Json("invalid surrogate pair".into()))
        } else {
            char::from_u32(hi).ok_or_else(|| Error::Json(format!("invalid \\u{hi:04x}")))
        }
    }

    fn parse_hex4(&mut self) -> Result<u32> {
        if self.pos + 4 > self.bytes.len() {
            return Err(Error::Json("short \\u escape".into()));
        }
        let mut value = 0u32;
        for _ in 0..4 {
            let c = self.bytes[self.pos];
            let d = (c as char)
                .to_digit(16)
                .ok_or_else(|| Error::Json("bad hex digit".into()))?;
            value = value * 16 + d;
            self.pos += 1;
        }
        Ok(value)
    }

    fn parse_bool(&mut self) -> Result<JsonValue> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(JsonValue::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(JsonValue::Bool(false))
        } else {
            Err(Error::Json(format!("invalid literal at {}", self.pos)))
        }
    }

    fn parse_null(&mut self) -> Result<JsonValue> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(JsonValue::Null)
        } else {
            Err(Error::Json(format!("invalid literal at {}", self.pos)))
        }
    }

    fn parse_number(&mut self) -> Result<JsonValue> {
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
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| Error::Json("invalid number".into()))?;
        if is_float {
            let f: f64 = text
                .parse()
                .map_err(|_| Error::Json(format!("invalid number '{text}'")))?;
            Ok(JsonValue::Float(f))
        } else {
            match text.parse::<i64>() {
                Ok(n) => Ok(JsonValue::Int(n)),
                Err(_) => match text.parse::<u64>() {
                    // Non-negative integers above i64::MAX keep full
                    // precision as unsigned literals.
                    Ok(u) => Ok(JsonValue::UInt(u)),
                    Err(_) => {
                        let f: f64 = text
                            .parse()
                            .map_err(|_| Error::Json(format!("invalid number '{text}'")))?;
                        Ok(JsonValue::Float(f))
                    }
                },
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Serializer
// ---------------------------------------------------------------------------

/// Serialize a JSON value with no extra whitespace.
pub fn to_string(value: &JsonValue) -> Result<String> {
    let mut out = String::new();
    write_value(&mut out, value)?;
    Ok(out)
}

/// Serialize a JSON value with two-space indentation.
pub fn to_string_pretty(value: &JsonValue) -> Result<String> {
    let mut out = String::new();
    write_pretty(&mut out, value, 0)?;
    out.push('\n');
    Ok(out)
}

fn write_value(out: &mut String, value: &JsonValue) -> Result<()> {
    match value {
        JsonValue::Null => out.push_str("null"),
        JsonValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        JsonValue::Int(n) => {
            write!(out, "{n}").map_err(|e| Error::Io(e.to_string()))?;
        }
        JsonValue::UInt(u) => {
            write!(out, "{u}").map_err(|e| Error::Io(e.to_string()))?;
        }
        JsonValue::Float(f) => write_float(out, *f)?,
        JsonValue::Str(s) => write_json_string(out, s),
        JsonValue::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item)?;
            }
            out.push(']');
        }
        JsonValue::Object(obj) => {
            out.push('{');
            for (i, (k, v)) in obj.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(out, k);
                out.push(':');
                write_value(out, v)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

fn write_pretty(out: &mut String, value: &JsonValue, indent: usize) -> Result<()> {
    match value {
        JsonValue::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return Ok(());
            }
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                push_indent(out, indent + 1);
                write_pretty(out, item, indent + 1)?;
                if i + 1 < items.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            push_indent(out, indent);
            out.push(']');
        }
        JsonValue::Object(obj) => {
            if obj.is_empty() {
                out.push_str("{}");
                return Ok(());
            }
            out.push_str("{\n");
            for (i, (k, v)) in obj.iter().enumerate() {
                push_indent(out, indent + 1);
                write_json_string(out, k);
                out.push_str(": ");
                write_pretty(out, v, indent + 1)?;
                if i + 1 < obj.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            push_indent(out, indent);
            out.push('}');
        }
        other => write_value(out, other)?,
    }
    Ok(())
}

fn push_indent(out: &mut String, indent: usize) {
    for _ in 0..indent {
        out.push_str("  ");
    }
}

fn write_float(out: &mut String, f: f64) -> Result<()> {
    if f.is_finite() {
        if f.fract() == 0.0 && f.abs() < 1e16 {
            write!(out, "{f:.1}").map_err(|e| Error::Io(e.to_string()))?;
        } else {
            write!(out, "{f}").map_err(|e| Error::Io(e.to_string()))?;
        }
    } else {
        // JSON has no NaN/Infinity; emit null rather than invalid JSON.
        out.push_str("null");
    }
    Ok(())
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
            '\u{000C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------------------
// Base64 (standard alphabet, with padding)
// ---------------------------------------------------------------------------

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard base64 encode with `=` padding.
pub fn base64_encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[((triple >> 18) & 0x3f) as usize] as char);
        out.push(B64_ALPHABET[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64_ALPHABET[((triple >> 6) & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(B64_ALPHABET[(triple & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

fn b64_decode_byte(c: u8) -> Option<u8> {
    match c {
        b'A'..=b'Z' => Some(c - b'A'),
        b'a'..=b'z' => Some(c - b'a' + 26),
        b'0'..=b'9' => Some(c - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// Standard base64 decode, requiring correct padding. Whitespace is
/// rejected so callers notice malformed input.
pub fn base64_decode(input: &str) -> Result<Vec<u8>> {
    let bytes = input.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err(Error::InvalidBase64(
            "length must be a multiple of 4".into(),
        ));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let mut sextets = [0u8; 4];
        let mut pad = 0;
        for (i, &c) in chunk.iter().enumerate() {
            if c == b'=' {
                sextets[i] = 0;
                pad += 1;
            } else {
                if pad > 0 {
                    return Err(Error::InvalidBase64("padding before end of group".into()));
                }
                sextets[i] = b64_decode_byte(c)
                    .ok_or_else(|| Error::InvalidBase64(format!("char '{c}'")))?;
            }
        }
        let triple = (u32::from(sextets[0]) << 18)
            | (u32::from(sextets[1]) << 12)
            | (u32::from(sextets[2]) << 6)
            | u32::from(sextets[3]);
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad < 1 {
            out.push(triple as u8);
        }
        if pad == 1 && sextets[3] != 0 {
            return Err(Error::InvalidBase64("non-zero bits in padding".into()));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_structured_json() {
        let doc = r#"{"a":1,"b":[true,false,null],"c":"héllo\n","d":-3.5}"#;
        let value = parse(doc).unwrap();
        let again = to_string(&value).unwrap();
        assert_eq!(parse(&again).unwrap(), value);
    }

    #[test]
    fn rejects_truncated_json() {
        assert!(parse(r#"{"a":1"#).is_err());
        assert!(parse(r#"[1,2"#).is_err());
        assert!(parse("").is_err());
    }

    #[test]
    fn base64_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        for input in [&b""[..], b"f", b"fo", b"foo", b"foob", b"hello world"] {
            let enc = base64_encode(input);
            assert_eq!(base64_decode(&enc).unwrap(), input);
        }
    }

    #[test]
    fn base64_rejects_bad_input() {
        assert!(base64_decode("Zg=").is_err());
        assert!(base64_decode("****").is_err());
    }

    #[test]
    fn integer_literal_keeps_precision() {
        assert_eq!(parse("42").unwrap(), JsonValue::Int(42));
        assert_eq!(parse("-7").unwrap(), JsonValue::Int(-7));
        assert_eq!(parse("1.5").unwrap(), JsonValue::Float(1.5));
    }

    #[test]
    fn large_unsigned_literal_keeps_full_u64_precision() {
        let text = "18446744073709551615"; // u64::MAX
        let value = parse(text).unwrap();
        assert_eq!(value, JsonValue::UInt(u64::MAX));
        assert_eq!(to_string(&value).unwrap(), text);
        // Values within i64 range stay signed.
        assert_eq!(
            parse("9223372036854775807").unwrap(),
            JsonValue::Int(i64::MAX)
        );
        assert_eq!(
            parse("9223372036854775808").unwrap(),
            JsonValue::UInt(i64::MAX as u64 + 1)
        );
        assert_eq!(parse("0").unwrap(), JsonValue::Int(0));
        assert_eq!(JsonValue::UInt(u64::MAX).as_int(), None);
        assert_eq!(JsonValue::UInt(u64::MAX).as_uint(), Some(u64::MAX));
    }
}
