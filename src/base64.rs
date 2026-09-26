//! Self-implemented Base64 (RFC 4648, standard alphabet, strict padding).
//!
//! Decoding rejects whitespace, unknown characters and non-canonical
//! padding, always with the offending character offset.

use core::fmt;

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// A Base64 decoding failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Base64Error {
    /// Character offset in the input string.
    pub offset: usize,
    pub message: String,
}

impl fmt::Display for Base64Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "base64 error at character {}: {}", self.offset, self.message)
    }
}

impl std::error::Error for Base64Error {}

/// Encode bytes to a padded Base64 string.
pub fn encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = u32::from(*chunk.get(1).unwrap_or(&0));
        let b2 = u32::from(*chunk.get(2).unwrap_or(&0));
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 0x3F] as char);
        out.push(ALPHABET[(n >> 12) as usize & 0x3F] as char);
        out.push(if chunk.len() > 1 { ALPHABET[(n >> 6) as usize & 0x3F] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[n as usize & 0x3F] as char } else { '=' });
    }
    out
}

fn decode_char(c: u8) -> Option<u8> {
    match c {
        b'A'..=b'Z' => Some(c - b'A'),
        b'a'..=b'z' => Some(c - b'a' + 26),
        b'0'..=b'9' => Some(c - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// Decode a Base64 string. Requires canonical form: length a multiple of
/// 4, `=` padding only in the final group, no whitespace.
pub fn decode(input: &str) -> Result<Vec<u8>, Base64Error> {
    let bytes = input.as_bytes();
    if bytes.len() % 4 != 0 {
        return Err(Base64Error {
            offset: bytes.len(),
            message: "length is not a multiple of 4 (missing padding?)".to_string(),
        });
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (group_idx, group) in bytes.chunks(4).enumerate() {
        let base = group_idx * 4;
        let last_group = base + 4 == bytes.len();
        let pad = group.iter().filter(|&&c| c == b'=').count();
        if pad > 0 {
            if !last_group {
                return Err(Base64Error {
                    offset: base + group.iter().position(|&c| c == b'=').unwrap_or(0),
                    message: "'=' padding is only allowed in the final group".to_string(),
                });
            }
            if pad > 2 || group[..4 - pad].iter().any(|&c| c == b'=') {
                return Err(Base64Error {
                    offset: base,
                    message: "malformed '=' padding".to_string(),
                });
            }
        }
        let mut n: u32 = 0;
        for (i, &c) in group.iter().enumerate() {
            let v = if i < 4 - pad {
                decode_char(c).ok_or_else(|| Base64Error {
                    offset: base + i,
                    message: format!("invalid base64 character 0x{:02X}", c),
                })?
            } else {
                0
            };
            n = (n << 6) | u32::from(v);
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
