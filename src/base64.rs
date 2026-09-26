//! Base64（RFC 4648，标准字母表，带填充）编解码，自行实现。

use crate::error::{Error, Result};

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn encode(data: &[u8]) -> String {
    let mut s = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        s.push(ALPHABET[(n >> 18) as usize & 63] as char);
        s.push(ALPHABET[(n >> 12) as usize & 63] as char);
        s.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        s.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    s
}

fn decode_char(c: u8) -> Result<u32> {
    match c {
        b'A'..=b'Z' => Ok((c - b'A') as u32),
        b'a'..=b'z' => Ok((c - b'a' + 26) as u32),
        b'0'..=b'9' => Ok((c - b'0' + 52) as u32),
        b'+' => Ok(62),
        b'/' => Ok(63),
        _ => Err(Error::Base64(format!("invalid character 0x{c:02x}"))),
    }
}

pub fn decode(s: &str) -> Result<Vec<u8>> {
    let bytes: Vec<u8> = s.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if !bytes.len().is_multiple_of(4) {
        return Err(Error::Base64("length is not a multiple of 4".into()));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (i, chunk) in bytes.chunks(4).enumerate() {
        let last = i == bytes.len() / 4 - 1;
        let pad = chunk.iter().filter(|&&c| c == b'=').count();
        if pad > 0 && !last {
            return Err(Error::Base64("padding in non-final chunk".into()));
        }
        if pad > 2 || chunk[..4 - pad].contains(&b'=') {
            return Err(Error::Base64("misplaced padding".into()));
        }
        let v: Vec<u32> = chunk[..4 - pad]
            .iter()
            .map(|&c| decode_char(c))
            .collect::<Result<_>>()?;
        let mut n: u32 = 0;
        for (j, &x) in v.iter().enumerate() {
            n |= x << (18 - 6 * j);
        }
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let cases: Vec<Vec<u8>> = vec![
            vec![],
            vec![b'f'],
            b"fo".to_vec(),
            b"foo".to_vec(),
            b"foob".to_vec(),
            b"fooba".to_vec(),
            b"foobar".to_vec(),
            (0u8..=255).collect(),
        ];
        for c in cases {
            assert_eq!(decode(&encode(&c)).unwrap(), c);
        }
    }

    #[test]
    fn rejects_bad_input() {
        assert!(decode("a").is_err());
        assert!(decode("a===").is_err());
        assert!(decode("ab=c").is_err());
        assert!(decode("****").is_err());
    }
}
