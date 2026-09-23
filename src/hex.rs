//! 十六进制编解码，用于 CLI / HTTP 接口表示二进制键值。

use crate::error::{Error, Result};

pub fn to_hex(data: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(data.len() * 2);
    for &b in data {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xF) as usize] as char);
    }
    s
}

pub fn from_hex(s: &str) -> Result<Vec<u8>> {
    let s = s.trim();
    if !s.len().is_multiple_of(2) {
        return Err(Error::bad_request("hex string must have even length"));
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    for pair in bytes.chunks(2) {
        let hi = hex_val(pair[0])?;
        let lo = hex_val(pair[1])?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

fn hex_val(c: u8) -> Result<u8> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(Error::bad_request(format!(
            "invalid hex character: {}",
            c as char
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        assert_eq!(to_hex(&[]), "");
        assert_eq!(from_hex("").unwrap(), Vec::<u8>::new());
        let data = vec![0x00, 0xde, 0xad, 0xbe, 0xef, 0xff];
        assert_eq!(to_hex(&data), "00deadbeefff");
        assert_eq!(from_hex("00DEADBEEFFF").unwrap(), data);
    }

    #[test]
    fn rejects_bad_input() {
        assert!(from_hex("abc").is_err()); // 奇数长度
        assert!(from_hex("zz").is_err()); // 非法字符
    }
}
