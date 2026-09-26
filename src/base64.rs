//! Minimal standard-alphabet base64 codec (RFC 4648), implemented from
//! scratch so the crate has no network/registry dependency.

use crate::error::{RbError, RbResult};

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Encode bytes to base64 text (with `=` padding).
pub fn encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
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

fn decode_char(c: u8) -> RbResult<u8> {
    match c {
        b'A'..=b'Z' => Ok(c - b'A'),
        b'a'..=b'z' => Ok(c - b'a' + 26),
        b'0'..=b'9' => Ok(c - b'0' + 52),
        b'+' => Ok(62),
        b'/' => Ok(63),
        _ => Err(RbError::InvalidInput(format!(
            "invalid base64 character 0x{c:02x}"
        ))),
    }
}

/// Decode padded or unpadded standard base64. ASCII whitespace is ignored.
pub fn decode(input: &str) -> RbResult<Vec<u8>> {
    // Keep indices into a compact (whitespace-free) copy so the final
    // group's padding can be inspected directly.
    let clean: Vec<u8> = input.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if clean.is_empty() {
        return Ok(Vec::new());
    }
    if clean.len() % 4 == 1 {
        return Err(RbError::InvalidInput(
            "base64 input has invalid length".into(),
        ));
    }

    // Determine padding: '=' may only occur at the very end.
    let pad_count = clean.iter().rev().take_while(|&&b| b == b'=').count();
    if pad_count > 2 {
        return Err(RbError::InvalidInput("too much base64 padding".into()));
    }
    let data_len = clean.len() - pad_count;
    for &c in &clean[..data_len] {
        decode_char(c)?;
    }
    // Non-final groups must never contain '='.
    if clean[..data_len].contains(&b'=') {
        return Err(RbError::InvalidInput("misplaced base64 padding".into()));
    }

    let mut out = Vec::with_capacity(clean.len() / 4 * 3 + 2);
    let full_groups = data_len / 4;
    let rem = data_len % 4;

    for g in 0..full_groups {
        let c0 = decode_char(clean[g * 4])? as u32;
        let c1 = decode_char(clean[g * 4 + 1])? as u32;
        let c2 = decode_char(clean[g * 4 + 2])? as u32;
        let c3 = decode_char(clean[g * 4 + 3])? as u32;
        let triple = (c0 << 18) | (c1 << 12) | (c2 << 6) | c3;
        out.push((triple >> 16) as u8);
        out.push((triple >> 8) as u8);
        out.push(triple as u8);
    }

    // Final padded/unpadded group.
    if pad_count > 0 || rem > 0 {
        let base = full_groups * 4;
        match (rem, pad_count) {
            (0, 0) => {}
            (2, 2) | (2, 0) => {
                let c0 = decode_char(clean[base])? as u32;
                let c1 = decode_char(clean[base + 1])? as u32;
                let triple = (c0 << 18) | (c1 << 12);
                out.push((triple >> 16) as u8);
            }
            (3, 1) | (3, 0) => {
                let c0 = decode_char(clean[base])? as u32;
                let c1 = decode_char(clean[base + 1])? as u32;
                let c2 = decode_char(clean[base + 2])? as u32;
                let triple = (c0 << 18) | (c1 << 12) | (c2 << 6);
                out.push((triple >> 16) as u8);
                out.push((triple >> 8) as u8);
            }
            other => {
                return Err(RbError::InvalidInput(format!(
                    "invalid base64 final group (rem={}, pad={})",
                    other.0, other.1
                )))
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// RFC 4648 known vectors plus boundary lengths.
    #[test]
    fn rfc4648_vectors() {
        assert_eq!(encode(b""), "");
        assert_eq!(encode(b"f"), "Zg==");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"foob"), "Zm9vYg==");
        assert_eq!(encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(encode(b"foobar"), "Zm9vYmFy");
        for n in 0..200usize {
            let data: Vec<u8> = (0..n as u8).map(|i| i.wrapping_mul(37)).collect();
            let enc = encode(&data);
            assert_eq!(decode(&enc).unwrap(), data, "padded roundtrip n={n}");
            let stripped = enc.trim_end_matches('=');
            assert_eq!(decode(stripped).unwrap(), data, "unpadded n={n}");
        }
    }

    #[test]
    fn rejects_garbage() {
        assert!(decode("!!!!").is_err());
        assert!(decode("Zg===").is_err()); // too much padding
        assert!(decode("Z===").is_err()); // misplaced padding
        assert!(decode("A").is_err()); // invalid total length
        assert!(decode("Zm 8").is_ok()); // whitespace tolerated
    }
}
