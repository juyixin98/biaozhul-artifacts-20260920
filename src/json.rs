//! 最小零依赖 JSON 实现：解析、转义与序列化。
//!
//! 仅支持控制入口所需的子集：null、true/false、整数/浮点 number、string（含
//! `\uXXXX` 与代理对）、array、object。对象键保持插入顺序。

use std::fmt::Write as _;

use crate::error::{Error, Result};

/// JSON 值。对象用有序 `Vec` 保存以保持插入顺序。
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    /// 以原始词法保留的数字，序列化时原样输出；需要时用 [`Value::as_u64`] 转换。
    Num(String),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

impl Value {
    /// 取对象字段（可点路径，如 `"limits.max_rows"`）。
    pub fn get<'a>(&'a self, key: &str) -> Option<&'a Value> {
        let mut cur = self;
        for part in key.split('.') {
            match cur {
                Value::Obj(entries) => {
                    cur = entries.iter().find(|(k, _)| k == part).map(|(_, v)| v)?;
                }
                _ => return None,
            }
        }
        Some(cur)
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Num(s) => s.parse().ok(),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }
}

/// 解析 JSON 文本。
pub fn parse(input: &str) -> Result<Value> {
    let bytes = input.as_bytes();
    let mut p = Parser { b: bytes, pos: 0 };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.b.len() {
        return Err(Error::BadJson(format!("trailing data at byte {}", p.pos)));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn err<T>(&self, msg: &str) -> Result<T> {
        Err(Error::BadJson(format!("{msg} at byte {}", self.pos)))
    }

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

    fn parse_value(&mut self) -> Result<Value> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(Value::Str(self.parse_string()?)),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => self.err(&format!("unexpected byte {c:#x}")),
            None => self.err("unexpected end of input"),
        }
    }

    fn parse_object(&mut self) -> Result<Value> {
        self.pos += 1; // {
        let mut entries = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Obj(entries));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return self.err("expected string key");
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return self.err("expected ':'");
            }
            self.pos += 1;
            let val = self.parse_value()?;
            entries.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Value::Obj(entries));
                }
                _ => return self.err("expected ',' or '}'"),
            }
        }
    }

    fn parse_array(&mut self) -> Result<Value> {
        self.pos += 1; // [
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Arr(items));
        }
        loop {
            let val = self.parse_value()?;
            items.push(val);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Value::Arr(items));
                }
                _ => return self.err("expected ',' or ']'"),
            }
        }
    }

    fn parse_bool(&mut self) -> Result<Value> {
        if self.b[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Value::Bool(true))
        } else if self.b[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Value::Bool(false))
        } else {
            self.err("invalid literal")
        }
    }

    fn parse_null(&mut self) -> Result<Value> {
        if self.b[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Value::Null)
        } else {
            self.err("invalid literal")
        }
    }

    fn parse_number(&mut self) -> Result<Value> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        // 整数部分：要么是单个 '0'，要么以 1-9 开头；禁止前导零。
        let first = self.peek();
        match first {
            Some(b'0') => {
                self.pos += 1;
                if matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    return self.err("leading zeros not allowed");
                }
            }
            Some(c) if (b'1'..=b'9').contains(&c) => {
                self.pos += 1;
                while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                    self.pos += 1;
                }
            }
            _ => return self.err("invalid number"),
        }
        // 小数部分：'.' 后至少一位数字。
        if matches!(self.peek(), Some(b'.')) {
            self.pos += 1;
            let frac_start = self.pos;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
            if self.pos == frac_start {
                return self.err("number ends with '.'");
            }
        }
        // 指数部分：e/E 后可选符号，至少一位数字。
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            let exp_start = self.pos;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
            if self.pos == exp_start {
                return self.err("exponent has no digits");
            }
        }
        let lexeme = std::str::from_utf8(&self.b[start..self.pos])
            .map_err(|_| Error::BadJson("non-UTF8 number".into()))?;
        Ok(Value::Num(lexeme.to_owned()))
    }

    fn parse_string(&mut self) -> Result<String> {
        self.pos += 1; // 开引号
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err(Error::BadJson("unterminated string".into())),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let e = self.peek().ok_or(Error::BadJson("bad escape".into()))?;
                    self.pos += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000C}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let cp = self.parse_hex4()?;
                            if (0xD800..=0xDBFF).contains(&cp) {
                                // 高代理项，后面必须是 \uXXXX 低代理项。
                                if self.peek() != Some(b'\\') {
                                    return Err(Error::BadJson("expected low surrogate".into()));
                                }
                                self.pos += 1;
                                if self.peek() != Some(b'u') {
                                    return Err(Error::BadJson("expected low surrogate".into()));
                                }
                                self.pos += 1;
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err(Error::BadJson("bad low surrogate".into()));
                                }
                                let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                out.push(char::from_u32(c).ok_or_else(|| {
                                    Error::BadJson("invalid surrogate pair".into())
                                })?);
                            } else if (0xDC00..=0xDFFF).contains(&cp) {
                                return Err(Error::BadJson("lone low surrogate".into()));
                            } else {
                                out.push(
                                    char::from_u32(cp)
                                        .ok_or(Error::BadJson("invalid codepoint".into()))?,
                                );
                            }
                        }
                        _ => return Err(Error::BadJson(format!("bad escape \\{e}"))),
                    }
                }
                // 控制字符（U+0000..001F）在 JSON 字符串中必须转义。
                Some(c) if c < 0x20 => {
                    return Err(Error::BadJson("unescaped control character".into()))
                }
                Some(_) => {
                    // 复制一个 UTF-8 字符：按首字节确定长度。
                    let c = self.b[self.pos];
                    let len = if c < 0x80 {
                        1
                    } else if c >> 5 == 0b110 {
                        2
                    } else if c >> 4 == 0b1110 {
                        3
                    } else if c >> 3 == 0b11110 {
                        4
                    } else {
                        return Err(Error::BadJson("invalid UTF-8 lead byte".into()));
                    };
                    if self.pos + len > self.b.len() {
                        return Err(Error::BadJson("truncated UTF-8".into()));
                    }
                    let chunk = &self.b[self.pos..self.pos + len];
                    let s = std::str::from_utf8(chunk)
                        .map_err(|_| Error::BadJson("invalid UTF-8".into()))?;
                    out.push_str(s);
                    self.pos += len;
                }
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u32> {
        if self.pos + 4 > self.b.len() {
            return Err(Error::BadJson("short \\u escape".into()));
        }
        let mut v = 0u32;
        for _ in 0..4 {
            let c = self.b[self.pos];
            let d = match c {
                b'0'..=b'9' => u32::from(c - b'0'),
                b'a'..=b'f' => u32::from(c - b'a') + 10,
                b'A'..=b'F' => u32::from(c - b'A') + 10,
                _ => return Err(Error::BadJson("bad hex digit".into())),
            };
            v = (v << 4) | d;
            self.pos += 1;
        }
        Ok(v)
    }
}

/// 将 JSON 值序列化为紧凑字符串。
pub fn to_string(v: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, v);
    out
}

/// 将 JSON 值序列化为带缩进的字符串。
pub fn to_string_pretty(v: &Value, indent: u8) -> String {
    let mut out = String::new();
    write_value_pretty(&mut out, v, 0, indent);
    out.push('\n');
    out
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Num(s) => out.push_str(s),
        Value::Str(s) => write_json_string(out, s),
        Value::Arr(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Obj(o) => {
            out.push('{');
            for (i, (k, val)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(out, k);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

fn write_value_pretty(out: &mut String, v: &Value, depth: u8, indent: u8) {
    let pad = " ".repeat((depth as usize) * indent as usize);
    let child_pad = " ".repeat((depth as usize + 1) * indent as usize);
    match v {
        Value::Arr(a) if a.is_empty() => out.push_str("[]"),
        Value::Obj(o) if o.is_empty() => out.push_str("{}"),
        Value::Arr(a) => {
            out.push_str("[\n");
            for (i, item) in a.iter().enumerate() {
                out.push_str(&child_pad);
                write_value_pretty(out, item, depth + 1, indent);
                if i + 1 < a.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&pad);
            out.push(']');
        }
        Value::Obj(o) => {
            out.push_str("{\n");
            for (i, (k, val)) in o.iter().enumerate() {
                out.push_str(&child_pad);
                write_json_string(out, k);
                out.push_str(": ");
                write_value_pretty(out, val, depth + 1, indent);
                if i + 1 < o.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&pad);
            out.push('}');
        }
        other => write_value(out, other),
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_structures() {
        let raw = r#"{"op":"encode","rows":["a",null,""],"limits":{"max_rows":10}}"#;
        let v = parse(raw).unwrap();
        assert_eq!(v.get("op").and_then(Value::as_str), Some("encode"));
        assert_eq!(v.get("limits.max_rows").and_then(Value::as_u64), Some(10));
        let rows = v.get("rows").unwrap().as_array().unwrap();
        assert_eq!(rows[1], Value::Null);
        assert_eq!(rows[2].as_str(), Some(""));
        // 紧凑往返后重新解析应相等。
        let again = parse(&to_string(&v)).unwrap();
        assert_eq!(again, v);
    }

    #[test]
    fn unicode_escape_and_surrogate() {
        let v = parse(r#""你好 é 😀""#).unwrap();
        assert_eq!(v.as_str(), Some("你好 é 😀"));
    }

    #[test]
    fn rejects_bad() {
        assert!(parse(r#"{"a":}"#).is_err());
        assert!(parse(r#"[1 2]"#).is_err());
        assert!(parse(r#""bad"#).is_err());
        assert!(parse(r#""\ud800x""#).is_err());
        assert!(parse("01").is_err());
    }
}
