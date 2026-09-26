//! A deliberately small, dependency-free JSON parser and serializer — just
//! enough for the control entry. The parser rejects anything that is not
//! strict JSON (no comments, no trailing commas, no NaN/Infinity).

use crate::error::{RbError, RbResult};

/// JSON value.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    /// null
    Null,
    /// true / false
    Bool(bool),
    /// Number kept both as f64 (for echo) and its raw spelling.
    Num(f64),
    /// JSON string (already unescaped).
    Str(String),
    /// JSON array.
    Arr(Vec<Json>),
    /// JSON object, preserving insertion order.
    Obj(Vec<(String, Json)>),
}

impl Json {
    /// Object field by key.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// String field.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// u32 field (rejects negatives, fractions, out-of-range values).
    pub fn as_u32(&self) -> Option<u32> {
        match self {
            Json::Num(n) if *n >= 0.0 && *n <= u32::MAX as f64 && n.fract() == 0.0 => {
                Some(*n as u32)
            }
            _ => None,
        }
    }

    /// u64 field.
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Num(n) if *n >= 0.0 && *n <= u64::MAX as f64 && n.fract() == 0.0 => {
                Some(*n as u64)
            }
            _ => None,
        }
    }

    /// Array view.
    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Arr(a) => Some(a),
            _ => None,
        }
    }
}

/// Parse a JSON document. Any trailing non-whitespace is an error.
pub fn parse(input: &str) -> RbResult<Json> {
    let bytes = input.as_bytes();
    let mut p = Parser { bytes, pos: 0 };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != bytes.len() {
        return Err(RbError::InvalidInput(format!(
            "trailing data after JSON value at byte {}",
            p.pos
        )));
    }
    Ok(v)
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

    fn eat(&mut self, b: u8) -> RbResult<()> {
        if self.peek() == Some(b) {
            self.pos += 1;
            Ok(())
        } else {
            Err(RbError::InvalidInput(format!(
                "expected '{}' at byte {}",
                b as char, self.pos
            )))
        }
    }

    fn parse_value(&mut self) -> RbResult<Json> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(Json::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            other => Err(RbError::InvalidInput(format!(
                "unexpected byte {other:?} at {}",
                self.pos
            ))),
        }
    }

    fn parse_object(&mut self) -> RbResult<Json> {
        self.eat(b'{')?;
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Obj(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(RbError::InvalidInput(format!(
                    "expected string key at byte {}",
                    self.pos
                )));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            self.eat(b':')?;
            let value = self.parse_value()?;
            pairs.push((key, value));
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
                    return Err(RbError::InvalidInput(format!(
                        "expected ',' or '}}' at byte {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Obj(pairs))
    }

    fn parse_array(&mut self) -> RbResult<Json> {
        self.eat(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Arr(items));
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
                    return Err(RbError::InvalidInput(format!(
                        "expected ',' or ']' at byte {}",
                        self.pos
                    )))
                }
            }
        }
        Ok(Json::Arr(items))
    }

    fn parse_string(&mut self) -> RbResult<String> {
        self.eat(b'"')?;
        // Literal source bytes (including whole multibyte UTF-8 sequences)
        // are copied verbatim; escapes are re-encoded as UTF-8. The final
        // vector is validated as UTF-8 once.
        let mut raw: Vec<u8> = Vec::new();
        loop {
            if self.pos >= self.bytes.len() {
                return Err(RbError::InvalidInput("unterminated JSON string".into()));
            }
            let c = self.bytes[self.pos];
            self.pos += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = *self.bytes.get(self.pos).ok_or_else(|| {
                        RbError::InvalidInput("dangling escape in JSON string".into())
                    })?;
                    self.pos += 1;
                    match e {
                        b'"' => raw.push(b'"'),
                        b'\\' => raw.push(b'\\'),
                        b'/' => raw.push(b'/'),
                        b'b' => raw.push(0x08),
                        b'f' => raw.push(0x0c),
                        b'n' => raw.push(b'\n'),
                        b'r' => raw.push(b'\r'),
                        b't' => raw.push(b'\t'),
                        b'u' => {
                            let cp = self.parse_hex4()?;
                            // Surrogate pair handling.
                            let cp = if (0xD800..=0xDBFF).contains(&cp) {
                                if self.bytes.get(self.pos) == Some(&b'\\')
                                    && self.bytes.get(self.pos + 1) == Some(&b'u')
                                {
                                    self.pos += 2;
                                    let low = self.parse_hex4()?;
                                    if !(0xDC00..=0xDFFF).contains(&low) {
                                        return Err(RbError::InvalidInput(
                                            "bad low surrogate".into(),
                                        ));
                                    }
                                    0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                                } else {
                                    return Err(RbError::InvalidInput(
                                        "dangling high surrogate".into(),
                                    ));
                                }
                            } else {
                                cp
                            };
                            let ch = char::from_u32(cp).ok_or_else(|| {
                                RbError::InvalidInput("invalid unicode escape".into())
                            })?;
                            let mut buf = [0u8; 4];
                            raw.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        other => {
                            return Err(RbError::InvalidInput(format!(
                                "invalid escape \\{}",
                                other as char
                            )))
                        }
                    }
                }
                0..=0x1f => {
                    return Err(RbError::InvalidInput(
                        "unescaped control byte in JSON string".into(),
                    ))
                }
                _ => raw.push(c),
            }
        }
        String::from_utf8(raw)
            .map_err(|_| RbError::InvalidInput("invalid UTF-8 in JSON string".into()))
    }

    fn parse_hex4(&mut self) -> RbResult<u32> {
        let mut v = 0u32;
        for _ in 0..4 {
            let c = *self
                .bytes
                .get(self.pos)
                .ok_or_else(|| RbError::InvalidInput("short unicode escape".into()))?;
            self.pos += 1;
            let d = match c {
                b'0'..=b'9' => u32::from(c - b'0'),
                b'a'..=b'f' => u32::from(c - b'a') + 10,
                b'A'..=b'F' => u32::from(c - b'A') + 10,
                _ => return Err(RbError::InvalidInput("bad hex digit".into())),
            };
            v = (v << 4) | d;
        }
        Ok(v)
    }

    fn parse_bool(&mut self) -> RbResult<Json> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err(RbError::InvalidInput(format!(
                "invalid literal at byte {}",
                self.pos
            )))
        }
    }

    fn parse_null(&mut self) -> RbResult<Json> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err(RbError::InvalidInput(format!(
                "invalid literal at byte {}",
                self.pos
            )))
        }
    }

    fn parse_number(&mut self) -> RbResult<Json> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let mut saw_digit = false;
        let first_digit_pos = self.pos;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                saw_digit = true;
                self.pos += 1;
            } else {
                break;
            }
        }
        // Strict JSON: a leading zero may not be followed by more digits.
        if self.bytes.get(first_digit_pos) == Some(&b'0') && self.pos > first_digit_pos + 1 {
            return Err(RbError::InvalidInput(format!(
                "leading zeros in number at byte {first_digit_pos}"
            )));
        }
        // Fraction.
        if self.peek() == Some(b'.') {
            self.pos += 1;
            while let Some(c) = self.peek() {
                if c.is_ascii_digit() {
                    self.pos += 1;
                } else {
                    break;
                }
            }
        }
        // Exponent.
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            while let Some(c) = self.peek() {
                if c.is_ascii_digit() {
                    self.pos += 1;
                } else {
                    break;
                }
            }
        }
        if !saw_digit {
            return Err(RbError::InvalidInput(format!(
                "invalid number at byte {start}"
            )));
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| RbError::InvalidInput("invalid utf-8 in number".into()))?;
        let n: f64 = text
            .parse()
            .map_err(|_| RbError::InvalidInput(format!("invalid number {text}")))?;
        Ok(Json::Num(n))
    }
}

/// Serialize a [`Json`] value compactly.
pub fn stringify(v: &Json) -> String {
    let mut out = String::new();
    write_into(v, &mut out);
    out
}

fn write_into(v: &Json, out: &mut String) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Num(n) => {
            if n.fract() == 0.0 && n.is_finite() && n.abs() < 1e16 {
                out.push_str(&format!("{}", *n as i64));
            } else {
                out.push_str(&n.to_string());
            }
        }
        Json::Str(s) => write_json_string(s, out),
        Json::Arr(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_into(item, out);
            }
            out.push(']');
        }
        Json::Obj(pairs) => {
            out.push('{');
            for (i, (k, val)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(k, out);
                out.push(':');
                write_into(val, out);
            }
            out.push('}');
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
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_all_types() {
        let doc = r#"{"a":1,"b":-2.5,"c":"x\ny","d":true,"e":null,"f":[1,[2],{}],"g":"é\u00e9","h":"\uD83D\uDE00"}"#;
        let v = parse(doc).unwrap();
        assert_eq!(v.get("a").and_then(Json::as_u32), Some(1));
        assert_eq!(v.get("d"), Some(&Json::Bool(true)));
        assert_eq!(v.get("e"), Some(&Json::Null));
        assert_eq!(v.get("g").and_then(Json::as_str), Some("éé"));
        assert_eq!(v.get("h").and_then(Json::as_str), Some("😀"));
        // Compact reserialization then re-parse is stable.
        let again = parse(&stringify(&v)).unwrap();
        assert_eq!(again, v);
    }

    #[test]
    fn rejects_strict_json_violations() {
        for bad in [
            "{",
            "[] x",
            "{,}",
            "{\"a\":}",
            "[1,]",
            "01",
            "{\"a\":1,}",
            "\"unterminated",
            "\"bad \\q\"",
            "\"\\uD800x\"",
            "tru",
        ] {
            assert!(parse(bad).is_err(), "should reject: {bad}");
        }
    }

    #[test]
    fn large_u64_number_exact() {
        let v = parse("4294967295").unwrap();
        assert_eq!(v.as_u32(), Some(u32::MAX));
        assert!(parse("4294967296").unwrap().as_u32().is_none());
    }
}
