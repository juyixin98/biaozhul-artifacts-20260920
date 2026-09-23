//! Minimal JSON: just enough for the ingest/query HTTP API.
//! Supports objects, arrays, strings (with basic escapes), integers,
//! floats, booleans and null. Not a general-purpose parser.

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
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
    pub fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(a) => Some(a),
            _ => None,
        }
    }
}

pub fn parse(input: &str) -> Result<Json, String> {
    let bytes = input.as_bytes();
    let mut p = Parser { bytes, pos: 0 };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != bytes.len() {
        return Err(format!("trailing data at byte {}", p.pos));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r') {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Result<u8, String> {
        self.bytes.get(self.pos).copied().ok_or_else(|| "unexpected end".to_string())
    }

    fn expect(&mut self, b: u8) -> Result<(), String> {
        if self.peek()? == b {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected {:?} at byte {}", b as char, self.pos))
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        match self.peek()? {
            b'{' => self.object(),
            b'[' => self.array(),
            b'"' => Ok(Json::Str(self.string()?)),
            b't' => self.literal("true", Json::Bool(true)),
            b'f' => self.literal("false", Json::Bool(false)),
            b'n' => self.literal("null", Json::Null),
            _ => self.number(),
        }
    }

    fn literal(&mut self, word: &str, v: Json) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(v)
        } else {
            Err(format!("invalid literal at byte {}", self.pos))
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek()? == b'-' {
            self.pos += 1;
        }
        let mut is_float = false;
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b'0'..=b'9' => self.pos += 1,
                b'.' | b'e' | b'E' | b'+' | b'-' => {
                    is_float = true;
                    self.pos += 1;
                }
                _ => break,
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| "bad number")?;
        if text.is_empty() || text == "-" {
            return Err(format!("invalid number at byte {}", start));
        }
        if !is_float {
            if let Ok(i) = text.parse::<i64>() {
                return Ok(Json::Int(i));
            }
        }
        text.parse::<f64>()
            .map(Json::Float)
            .map_err(|_| format!("invalid number {:?} at byte {}", text, start))
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let b = self.peek()?;
            self.pos += 1;
            match b {
                b'"' => return Ok(out),
                b'\\' => {
                    let e = self.peek()?;
                    self.pos += 1;
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
                            let hex = self
                                .bytes
                                .get(self.pos..self.pos + 4)
                                .ok_or("bad \\u escape")?;
                            let code = u32::from_str_radix(
                                std::str::from_utf8(hex).map_err(|_| "bad \\u escape")?,
                                16,
                            )
                            .map_err(|_| "bad \\u escape")?;
                            self.pos += 4;
                            out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                        }
                        _ => return Err(format!("bad escape at byte {}", self.pos)),
                    }
                }
                _ => {
                    // collect UTF-8 continuation bytes as-is
                    let mut buf = vec![b];
                    while self.pos < self.bytes.len() && self.bytes[self.pos] & 0xC0 == 0x80 {
                        buf.push(self.bytes[self.pos]);
                        self.pos += 1;
                    }
                    out.push_str(
                        std::str::from_utf8(&buf).map_err(|_| "invalid utf-8 in string")?,
                    );
                }
            }
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek()? == b']' {
            self.pos += 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek()? {
                b',' => self.pos += 1,
                b']' => {
                    self.pos += 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.pos)),
            }
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        self.expect(b'{')?;
        let mut kv = Vec::new();
        self.skip_ws();
        if self.peek()? == b'}' {
            self.pos += 1;
            return Ok(Json::Obj(kv));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            self.skip_ws();
            let val = self.value()?;
            kv.push((key, val));
            self.skip_ws();
            match self.peek()? {
                b',' => self.pos += 1,
                b'}' => {
                    self.pos += 1;
                    return Ok(Json::Obj(kv));
                }
                _ => return Err(format!("expected ',' or '}}' at byte {}", self.pos)),
            }
        }
    }
}

/// Escapes a string for JSON output.
pub fn escape(s: &str) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ingest_body() {
        let v = parse(r#"{"series":"cpu","points":[[1,2],[3,-4],[9223372036854775807,0]]}"#).unwrap();
        assert_eq!(v.get("series").unwrap().as_str().unwrap(), "cpu");
        let pts = v.get("points").unwrap().as_arr().unwrap();
        assert_eq!(pts.len(), 3);
        assert_eq!(
            pts[2].as_arr().unwrap()[0].as_i64().unwrap(),
            i64::MAX
        );
    }

    #[test]
    fn parse_errors() {
        assert!(parse("{").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse("{\"a\":}").is_err());
    }
}
