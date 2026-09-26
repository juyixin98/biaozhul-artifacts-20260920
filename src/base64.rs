//! Standard Base64 (RFC 4648) codec for the `bytes` wire type and the
//! JSON `--data` envelope. Hand-written, no dependencies.

use crate::error::{Error, Result};

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Encode bytes to a base64 string (with padding).
pub fn encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
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

/// Decode a padded or unpadded standard-alphabet base64 string. Whitespace
/// (including newlines) is ignored.
pub fn decode(input: &str) -> Result<Vec<u8>> {
    let mut digits: Vec<u8> = Vec::with_capacity(input.len());
    for c in input.bytes() {
        if matches!(c, b' ' | b'\t' | b'\n' | b'\r') {
            continue;
        }
        if c == b'=' {
            break; // padding: remainder must be padding only
        }
        let d = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => {
                return Err(Error::InvalidValue(format!(
                    "invalid base64 character {c:?}"
                )))
            }
        };
        digits.push(d);
    }
    if digits.len() % 4 == 1 {
        return Err(Error::InvalidValue("invalid base64 length".into()));
    }
    let mut out = Vec::with_capacity(digits.len() * 3 / 4);
    for chunk in digits.chunks(4) {
        let mut triple = 0u32;
        for d in chunk {
            triple = (triple << 6) | *d as u32;
        }
        // Short final blocks carry their bits in the *high* positions; the
        // missing low sextets are zero padding.
        triple <<= 6 * (4 - chunk.len());
        out.push((triple >> 16) as u8);
        if chunk.len() >= 3 {
            out.push((triple >> 8) as u8);
        }
        if chunk.len() >= 4 {
            out.push(triple as u8);
        }
    }
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
    }

    #[test]
    fn roundtrip() {
        let data = vec![0u8, 1, 2, 250, 251, 252, 253, 254, 255, 128, 64];
        let s = encode(&data);
        assert_eq!(decode(&s).unwrap(), data);
        // Whitespace tolerated.
        assert_eq!(decode("Zm9v\nYmFy").unwrap(), b"foobar");
    }

    #[test]
    fn rejects_bad() {
        assert!(decode("A===").is_err());
        assert!(decode("****").is_err());
    }
}
