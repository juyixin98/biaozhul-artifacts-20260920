//! Base64 (RFC 4648, standard alphabet, with padding), implemented locally so
//! the JSON control entry does not depend on an external codec.

use crate::error::Error;

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Encode bytes into a base64 string with padding.
pub fn encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

fn decode_char(c: u8) -> Result<u32, Error> {
    match c {
        b'A'..=b'Z' => Ok(u32::from(c - b'A')),
        b'a'..=b'z' => Ok(u32::from(c - b'a') + 26),
        b'0'..=b'9' => Ok(u32::from(c - b'0') + 52),
        b'+' => Ok(62),
        b'/' => Ok(63),
        _ => Err(Error::Base64(format!("invalid character {:#04x}", c))),
    }
}

/// Decode a base64 string. Whitespace is not allowed; padding must be correct.
pub fn decode(s: &str) -> Result<Vec<u8>, Error> {
    let bytes = s.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err(Error::Base64("length not a multiple of 4".into()));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (ci, chunk) in bytes.chunks(4).enumerate() {
        let last_chunk = ci == bytes.len() / 4 - 1;
        let pad = chunk.iter().filter(|&&c| c == b'=').count();
        if pad > 0 {
            // Padding is only allowed at the very end, at most two characters,
            // and only in the trailing positions of the final chunk.
            if !last_chunk || pad > 2 || chunk[..4 - pad].contains(&b'=') {
                return Err(Error::Base64("misplaced padding".into()));
            }
        }
        let v0 = decode_char(chunk[0])?;
        let v1 = decode_char(chunk[1])?;
        let v2 = if pad >= 2 { 0 } else { decode_char(chunk[2])? };
        let v3 = if pad >= 1 { 0 } else { decode_char(chunk[3])? };
        let n = (v0 << 18) | (v1 << 12) | (v2 << 6) | v3;
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
        for data in [
            &b""[..],
            b"f",
            b"fo",
            b"foo",
            b"foob",
            b"fooba",
            b"foobar",
            &[0, 1, 2, 253, 254, 255][..],
        ] {
            assert_eq!(decode(&encode(data)).unwrap(), data);
        }
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"f"), "Zg==");
    }

    #[test]
    fn rejects_bad_input() {
        assert!(decode("Zg=").is_err()); // bad length
        assert!(decode("Zg==Zg==").is_err()); // padding mid-stream
        assert!(decode("Z!==").is_err()); // invalid char
        assert!(decode("====").is_err()); // all padding
    }
}
