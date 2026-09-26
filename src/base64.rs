//! Small, dependency-free standard-alphabet Base64 codec (RFC 4648).
//!
//! Used only to carry file payloads inside JSON control documents. The
//! decoder is strict: it rejects embedded whitespace, bad padding and any
//! character outside the canonical alphabet.

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

/// Encode bytes to canonical padded Base64.
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

/// Decode strict padded Base64. Returns an error on any invalid byte,
/// malformed padding, or non-zero pad bits.
pub fn decode(input: &str) -> Result<Vec<u8>, String> {
    let bytes = input.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err("base64 length is not a multiple of 4".to_string());
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    let mut i = 0;
    while i < bytes.len() {
        let mut vals = [0u8; 4];
        let mut pad = 0;
        for j in 0..4 {
            let c = bytes[i + j];
            if c == b'=' {
                pad += 1;
                // Padding may only appear in positions 3 and 4 of the group.
                if j < 2 {
                    return Err("unexpected '='".to_string());
                }
                vals[j] = 0;
            } else {
                if pad > 0 {
                    return Err("data after padding".to_string());
                }
                vals[j] = decode_char(c).ok_or_else(|| "invalid base64 character".to_string())?;
            }
        }
        let triple = (vals[0] as u32) << 18
            | (vals[1] as u32) << 12
            | (vals[2] as u32) << 6
            | vals[3] as u32;
        out.push((triple >> 16) as u8);
        if pad == 0 {
            out.push((triple >> 8) as u8);
            out.push(triple as u8);
        } else if pad == 1 {
            // One padding byte: only the top 4 bits of the third 6-bit group
            // are used; its low 2 bits and the whole fourth group must be 0.
            if vals[2] & 0x03 != 0 || vals[3] != 0 {
                return Err("non-zero pad bits".to_string());
            }
            out.push((triple >> 8) as u8);
        } else {
            // Two padding bytes: only the top 2 bits of the second 6-bit
            // group are used; its low 4 bits must be zero (canonical check).
            if vals[1] & 0x0f != 0 || vals[2] != 0 || vals[3] != 0 {
                return Err("non-zero pad bits".to_string());
            }
        }
        i += 4;
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
        for s in ["", "f", "fo", "foo", "foob", "fooba", "foobar"] {
            assert_eq!(decode(&encode(s.as_bytes())).unwrap(), s.as_bytes());
        }
    }

    #[test]
    fn rejects_garbage() {
        assert!(decode("Zg=").is_err());
        assert!(decode("====").is_err());
        assert!(decode("Zm9v\n").is_err());
        // non-zero pad bits
        assert!(decode("Zh==").is_err());
    }
}
