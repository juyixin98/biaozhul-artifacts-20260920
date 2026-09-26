//! 极简 JSON 解析/序列化（自行实现，仅覆盖控制入口所需的子集：
//! 对象、数组、字符串、数字、true/false/null，字符串含 \uXXXX 转义）。

use crate::error::{Error, Result};

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    /// 对象字段查找；非对象或不存在返回 None。
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

    /// 非负整数；小数、负数、越界均返回 None。
    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Json::Num(n) if *n >= 0.0 && n.fract() == 0.0 && *n <= u64::MAX as f64 => {
                Some(*n as u64)
            }
            _ => None,
        }
    }
}

/// 序列化字符串字段（含必要转义）。
pub fn escape_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
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
    out
}

pub fn parse(input: &str) -> Result<Json> {
    let mut p = Parser {
        b: input.as_bytes(),
        i: 0,
    };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(Error::Json("trailing characters".into()));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn err<T>(&self, msg: &str) -> Result<T> {
        Err(Error::Json(format!("{msg} at byte {}", self.i)))
    }

    fn ws(&mut self) {
        while self.i < self.b.len() && self.b[self.i].is_ascii_whitespace() {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn expect(&mut self, c: u8) -> Result<()> {
        if self.peek() == Some(c) {
            self.i += 1;
            Ok(())
        } else {
            self.err(&format!("expected '{}'", c as char))
        }
    }

    fn value(&mut self) -> Result<Json> {
        self.ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => self.lit("true", Json::Bool(true)),
            Some(b'f') => self.lit("false", Json::Bool(false)),
            Some(b'n') => self.lit("null", Json::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => self.err("unexpected character"),
        }
    }

    fn lit(&mut self, word: &str, v: Json) -> Result<Json> {
        if self.b[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            self.err("invalid literal")
        }
    }

    fn number(&mut self) -> Result<Json> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.i += 1;
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        let s = std::str::from_utf8(&self.b[start..self.i])
            .map_err(|_| Error::Json(format!("invalid number at byte {start}")))?;
        s.parse::<f64>()
            .map(Json::Num)
            .map_err(|_| Error::Json(format!("invalid number '{s}'")))
    }

    fn string(&mut self) -> Result<String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return self.err("unterminated string"),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.i += 1;
                    let e = self.peek().ok_or_else(|| {
                        Error::Json(format!("unterminated escape at byte {}", self.i))
                    })?;
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
                            let hi = self.hex4()?;
                            let cp = if (0xd800..0xdc00).contains(&hi) {
                                // 高代理项：要求紧跟 \uXXXX 低代理项
                                if self.peek() == Some(b'\\') {
                                    self.i += 1;
                                    self.expect(b'u')?;
                                    let lo = self.hex4()?;
                                    if !(0xdc00..0xe000).contains(&lo) {
                                        return self.err("invalid low surrogate");
                                    }
                                    0x10000 + ((hi - 0xd800) << 10) + (lo - 0xdc00)
                                } else {
                                    return self.err("lone high surrogate");
                                }
                            } else {
                                hi
                            };
                            out.push(char::from_u32(cp).ok_or_else(|| {
                                Error::Json(format!("invalid code point {cp:#x}"))
                            })?);
                        }
                        _ => return self.err("invalid escape"),
                    }
                }
                Some(c) if c < 0x20 => return self.err("control character in string"),
                Some(_) => {
                    // UTF-8 原样拷贝一个完整字符
                    let s = std::str::from_utf8(&self.b[self.i..])
                        .map_err(|_| Error::Json("invalid utf-8".into()))?;
                    let ch = s.chars().next().unwrap();
                    out.push(ch);
                    self.i += ch.len_utf8();
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u32> {
        if self.i + 4 > self.b.len() {
            return self.err("truncated \\u escape");
        }
        let mut v = 0u32;
        for _ in 0..4 {
            let c = self.b[self.i];
            let d = (c as char)
                .to_digit(16)
                .ok_or_else(|| Error::Json(format!("bad hex digit at byte {}", self.i)))?;
            v = v * 16 + d;
            self.i += 1;
        }
        Ok(v)
    }

    fn array(&mut self) -> Result<Json> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Json::Arr(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    return Ok(Json::Arr(items));
                }
                _ => return self.err("expected ',' or ']'"),
            }
        }
    }

    fn object(&mut self) -> Result<Json> {
        self.expect(b'{')?;
        let mut kv = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Json::Obj(kv));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return self.err("expected object key");
            }
            let k = self.string()?;
            self.ws();
            self.expect(b':')?;
            let v = self.value()?;
            kv.push((k, v));
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    return Ok(Json::Obj(kv));
                }
                _ => return self.err("expected ',' or '}'"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_request() {
        let j = parse(r#"{"op":"compress","window":4096,"input_base64":"aGk="}"#).unwrap();
        assert_eq!(j.get("op").and_then(Json::as_str), Some("compress"));
        assert_eq!(j.get("window").and_then(Json::as_u64), Some(4096));
        assert_eq!(j.get("input_base64").and_then(Json::as_str), Some("aGk="));
    }

    #[test]
    fn string_escapes() {
        let j = parse(r#""a\nbA😀""#).unwrap();
        assert_eq!(j.as_str(), Some("a\nbA😀"));
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse("{").is_err());
        assert!(parse(r#"{"a":}"#).is_err());
        assert!(parse("nul").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse("1 2").is_err());
    }
}
