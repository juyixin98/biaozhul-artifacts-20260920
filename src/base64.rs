//! base64（标准字母表，带 `=` 填充）编解码，用于 JSON 请求中承载二进制 `.cdc` 数据。

use crate::error::{Error, Result};

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn decode_char(b: u8) -> Option<u8> {
    match b {
        b'A'..=b'Z' => Some(b - b'A'),
        b'a'..=b'z' => Some(b - b'a' + 26),
        b'0'..=b'9' => Some(b - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// 将字节编码为 base64 字符串。
pub fn encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3f) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((triple >> 6) & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

/// 解码 base64；拒绝非法字符、非法填充与非零尾比特。
pub fn decode(input: &str) -> Result<Vec<u8>> {
    let bytes: Vec<u8> = input.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if !bytes.len().is_multiple_of(4) {
        return Err(Error::BadBase64("length is not a multiple of 4".into()));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    let mut i = 0;
    while i + 4 <= bytes.len() {
        let chunk = &bytes[i..i + 4];
        i += 4;
        let vals: [Option<u8>; 4] = [
            decode_char(chunk[0]),
            decode_char(chunk[1]),
            if chunk[2] == b'=' {
                None
            } else {
                decode_char(chunk[2])
            },
            if chunk[3] == b'=' {
                None
            } else {
                decode_char(chunk[3])
            },
        ];
        match (vals[0], vals[1], vals[2], vals[3]) {
            (Some(a), Some(b), Some(c), Some(d)) => {
                let triple =
                    ((a as u32) << 18) | ((b as u32) << 12) | ((c as u32) << 6) | (d as u32);
                out.push((triple >> 16) as u8);
                out.push((triple >> 8) as u8);
                out.push(triple as u8);
            }
            (Some(a), Some(b), Some(c), None) => {
                // 两个有效字符 + 两个 '='：最后 4 个尾比特必须为 0。
                let triple = ((a as u32) << 18) | ((b as u32) << 12) | ((c as u32) << 6);
                if triple & 0xf != 0 {
                    return Err(Error::BadBase64("non-zero trailing bits".into()));
                }
                out.push((triple >> 16) as u8);
                out.push((triple >> 8) as u8);
            }
            (Some(a), Some(b), None, None) => {
                let triple = ((a as u32) << 18) | ((b as u32) << 12);
                if triple & 0xff != 0 {
                    return Err(Error::BadBase64("non-zero trailing bits".into()));
                }
                out.push((triple >> 16) as u8);
            }
            _ => return Err(Error::BadBase64("illegal character or padding".into())),
        }
    }
    // 填充一旦出现就必须位于结尾：chunks_exact + 上面的分支已保证形态合法。
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc4648_vectors() {
        assert_eq!(encode(b""), "");
        assert_eq!(encode(b"f"), "Zg==");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"foob"), "Zm9vYg==");
        assert_eq!(encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(encode(b"foobar"), "Zm9vYmFy");
        for s in ["", "f", "fo", "foo", "foob", "fooba", "foobar"] {
            assert_eq!(decode(&encode(s.as_bytes())).unwrap(), s.as_bytes());
        }
    }

    #[test]
    fn rejects_bad() {
        assert!(decode("Zg=").is_err());
        assert!(decode("!!!!").is_err());
        assert!(decode("Zm9\nv").is_ok()); // 空白被忽略
    }
}
