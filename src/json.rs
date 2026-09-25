//! # 极简 JSON（零依赖）
//!
//! 仅实现 TCP 测试服务行协议所需的 JSON 子集：
//! object / array / string / number(i64 或 f64 语义，这里只用到整数) /
//! bool / null。刻意不使用 serde_json。
//!
//! 解析是严格的：拒绝尾随垃圾、拒绝重复键、字符串只接受合法转义。

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    /// 协议中所有数字都是整数（端口、偏移、时间戳……）；用 i64 表示。
    Int(i64),
    Str(String),
    Array(Vec<JsonValue>),
    /// BTreeMap 保证序列化输出确定（键排序）。
    Object(BTreeMap<String, JsonValue>),
}

impl JsonValue {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            JsonValue::Int(i) => Some(*i),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            JsonValue::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<JsonValue>> {
        match self {
            JsonValue::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&BTreeMap<String, JsonValue>> {
        match self {
            JsonValue::Object(o) => Some(o),
            _ => None,
        }
    }

    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        self.as_object().and_then(|o| o.get(key))
    }

    pub fn str_field(&self, key: &str) -> Result<&str, String> {
        self.get(key)
            .and_then(|v| v.as_str())
            .ok_or_else(|| format!("missing or non-string field `{key}`"))
    }

    pub fn int_field(&self, key: &str) -> Result<i64, String> {
        self.get(key)
            .and_then(|v| v.as_i64())
            .ok_or_else(|| format!("missing or non-integer field `{key}`"))
    }

    pub fn optional_str(&self, key: &str) -> Option<&str> {
        self.get(key).and_then(|v| v.as_str())
    }

    pub fn optional_i64(&self, key: &str) -> Option<i64> {
        self.get(key).and_then(|v| v.as_i64())
    }
}

/// 便捷构造宏风格的辅助函数。
pub fn obj(pairs: Vec<(&str, JsonValue)>) -> JsonValue {
    JsonValue::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn arr(items: Vec<JsonValue>) -> JsonValue {
    JsonValue::Array(items)
}

pub fn s(v: impl Into<String>) -> JsonValue {
    JsonValue::Str(v.into())
}

pub fn i(v: i64) -> JsonValue {
    JsonValue::Int(v)
}

pub fn b(v: bool) -> JsonValue {
    JsonValue::Bool(v)
}

/// 解析单个 JSON 值；输入必须恰好包含一个值（允许首尾空白）。
pub fn parse(input: &str) -> Result<JsonValue, String> {
    let mut p = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(format!("trailing garbage at byte {}", p.pos));
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

    fn parse_value(&mut self) -> Result<JsonValue, String> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => Ok(JsonValue::Str(self.parse_string()?)),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(b't') | Some(b'f') => self.parse_bool(),
            Some(b'n') => self.parse_null(),
            other => Err(format!(
                "unexpected character {other:?} at byte {}",
                self.pos
            )),
        }
    }

    fn parse_object(&mut self) -> Result<JsonValue, String> {
        self.pos += 1; // {
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonValue::Object(map));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(format!("expected string key at byte {}", self.pos));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(format!("expected `:` at byte {}", self.pos));
            }
            self.pos += 1;
            let value = self.parse_value()?;
            if map.insert(key.clone(), value).is_some() {
                return Err(format!("duplicate key `{key}`"));
            }
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err(format!("expected `,` or `}}` at byte {}", self.pos)),
            }
        }
        Ok(JsonValue::Object(map))
    }

    fn parse_array(&mut self) -> Result<JsonValue, String> {
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
                _ => return Err(format!("expected `,` or `]` at byte {}", self.pos)),
            }
        }
        Ok(JsonValue::Array(items))
    }

    fn parse_string(&mut self) -> Result<String, String> {
        self.pos += 1; // 开引号
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err("unterminated string".to_string()),
                Some(b'"') => {
                    self.pos += 1;
                    break;
                }
                Some(b'\\') => {
                    self.pos += 1;
                    match self.peek() {
                        Some(b'"') => out.push('"'),
                        Some(b'\\') => out.push('\\'),
                        Some(b'/') => out.push('/'),
                        Some(b'n') => out.push('\n'),
                        Some(b't') => out.push('\t'),
                        Some(b'r') => out.push('\r'),
                        Some(b'b') => out.push('\u{0008}'),
                        Some(b'f') => out.push('\u{000c}'),
                        Some(b'u') => {
                            self.pos += 1;
                            let cp = self.parse_hex4()?;
                            // 处理 UTF-16 代理对
                            let cp = if (0xD800..=0xDBFF).contains(&cp) {
                                if self.peek() != Some(b'\\') {
                                    return Err("expected low surrogate".to_string());
                                }
                                self.pos += 1;
                                if self.peek() != Some(b'u') {
                                    return Err("expected low surrogate".to_string());
                                }
                                self.pos += 1;
                                let lo = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&lo) {
                                    return Err("invalid low surrogate".to_string());
                                }
                                0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                            } else {
                                cp
                            };
                            match char::from_u32(cp) {
                                Some(c) => out.push(c),
                                None => return Err("invalid unicode escape".to_string()),
                            }
                            continue;
                        }
                        other => return Err(format!("bad escape {other:?}")),
                    }
                    self.pos += 1;
                }
                Some(c) if c < 0x20 => {
                    return Err(format!("unescaped control byte at {}", self.pos))
                }
                Some(_) => {
                    // 按 UTF-8 原样收集：找到下一个 ASCII 特殊字符边界。
                    let start = self.pos;
                    while self.pos < self.bytes.len()
                        && !matches!(self.bytes[self.pos], b'"' | b'\\')
                        && self.bytes[self.pos] >= 0x20
                    {
                        self.pos += 1;
                    }
                    match std::str::from_utf8(&self.bytes[start..self.pos]) {
                        Ok(slice) => out.push_str(slice),
                        Err(e) => return Err(format!("invalid utf-8 in string: {e}")),
                    }
                }
            }
        }
        Ok(out)
    }

    fn parse_hex4(&mut self) -> Result<u32, String> {
        if self.pos + 4 > self.bytes.len() {
            return Err("short unicode escape".to_string());
        }
        let s =
            std::str::from_utf8(&self.bytes[self.pos..self.pos + 4]).map_err(|e| e.to_string())?;
        let v = u32::from_str_radix(s, 16).map_err(|e| e.to_string())?;
        self.pos += 4;
        Ok(v)
    }

    fn parse_number(&mut self) -> Result<JsonValue, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }
        // 协议只允许整数；遇到小数点/指数直接拒绝，避免静默丢精度。
        if matches!(self.peek(), Some(b'.') | Some(b'e') | Some(b'E')) {
            return Err("floating-point numbers are not supported by this protocol".to_string());
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).unwrap();
        text.parse::<i64>()
            .map(JsonValue::Int)
            .map_err(|_| format!("invalid integer `{text}`"))
    }

    fn parse_bool(&mut self) -> Result<JsonValue, String> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(JsonValue::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(JsonValue::Bool(false))
        } else {
            Err(format!("bad literal at byte {}", self.pos))
        }
    }

    fn parse_null(&mut self) -> Result<JsonValue, String> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(JsonValue::Null)
        } else {
            Err(format!("bad literal at byte {}", self.pos))
        }
    }
}

/// 序列化为紧凑 JSON（无多余空白）。
pub fn to_string(v: &JsonValue) -> String {
    let mut out = String::new();
    write_value(&mut out, v);
    out
}

fn write_value(out: &mut String, v: &JsonValue) {
    match v {
        JsonValue::Null => out.push_str("null"),
        JsonValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        JsonValue::Int(i) => out.push_str(&i.to_string()),
        JsonValue::Str(s) => write_string(out, s),
        JsonValue::Array(items) => {
            out.push('[');
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        JsonValue::Object(map) => {
            out.push('{');
            for (idx, (k, val)) in map.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                write_string(out, k);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
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
    fn roundtrip_basic() {
        let input = r#"{"b":true,"a":[1,"x",null,false,-7]}"#;
        let v = parse(input).unwrap();
        assert_eq!(
            v.str_field("missing").unwrap_err(),
            "missing or non-string field `missing`"
        );
        assert_eq!(v.get("b").and_then(|x| x.as_bool()), Some(true));
        let a = v.get("a").unwrap().as_array().unwrap();
        assert_eq!(a[0].as_i64(), Some(1));
        assert_eq!(a[1].as_str(), Some("x"));
        assert_eq!(a[3].as_bool(), Some(false));
        assert_eq!(a[4].as_i64(), Some(-7));
        // 键被排序输出
        assert_eq!(to_string(&v), r#"{"a":[1,"x",null,false,-7],"b":true}"#);
    }

    #[test]
    fn rejects_bad_inputs() {
        assert!(parse("").is_err());
        assert!(parse("123 456").is_err());
        assert!(parse("{\"a\":1}x").is_err());
        assert!(parse("{\"a\":1,\"a\":2}").is_err()); // 重复键
        assert!(parse("1.5").is_err()); // 不支持浮点
        assert!(parse("\"unterminated").is_err());
        assert!(parse("[1,2,]").is_err()); // 不允许尾逗号
    }

    #[test]
    fn escapes_and_unicode() {
        let v = parse(r#""a\nbAé""#).unwrap();
        // a、换行、字面 b、A（U+0041）、é（U+00E9）
        assert_eq!(v.as_str(), Some("a\nbA\u{00e9}"));
    }
}
