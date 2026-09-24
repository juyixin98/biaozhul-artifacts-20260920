//! Small, dependency-free lowercase hex helpers.

/// Encode bytes as a lowercase hex string.
pub fn encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Decode a lowercase (or mixed-case) hex string. Fails on odd length or any
/// non-hex character.
pub fn decode(s: &str) -> Result<Vec<u8>, String> {
    if !s.len().is_multiple_of(2) {
        return Err("odd-length hex string".to_string());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = (bytes[i] as char).to_digit(16).ok_or("invalid hex digit")?;
        let lo = (bytes[i + 1] as char)
            .to_digit(16)
            .ok_or("invalid hex digit")?;
        out.push((hi * 16 + lo) as u8);
        i += 2;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        for v in [vec![], vec![0], vec![255, 0, 16, 171], (0u32..256).map(|i| i as u8).collect()] {
            assert_eq!(decode(&encode(&v)).unwrap(), v);
        }
        assert_eq!(encode(&[0xde, 0xad, 0xbe, 0xef]), "deadbeef");
    }

    #[test]
    fn errors() {
        assert!(decode("abc").is_err());
        assert!(decode("zz").is_err());
    }
}
