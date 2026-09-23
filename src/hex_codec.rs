//! 十六进制编解码。JSON 请求/响应中的字节（root、chunk、证明哈希）均用小写 hex。

use std::fmt::Write;

/// 字节序列转小写十六进制字符串。
pub fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{:02x}", b);
    }
    s
}

/// 解析十六进制字符串，非法字符或奇数长度时返回 `None`。
pub fn from_hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_digit(bytes[i])?;
        let lo = hex_digit(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Some(out)
}

fn hex_digit(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        for v in [vec![], vec![0u8], vec![0xff, 0x00, 0xa5]] {
            assert_eq!(from_hex(&to_hex(&v)).unwrap(), v);
        }
        assert!(from_hex("abc").is_none());
        assert!(from_hex("zz").is_none());
    }
}
