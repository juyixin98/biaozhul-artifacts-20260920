//! Hex helpers used by the JSON control protocol and the tests.

/// Encode bytes as lowercase hex.
pub fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0F) as usize] as char);
    }
    s
}

/// Decode a hex string (upper or lower case, optional whitespace ignored).
pub fn from_hex(s: &str) -> Result<Vec<u8>, String> {
    let nibbles: Vec<u8> = s
        .bytes()
        .filter(|b| !b.is_ascii_whitespace())
        .map(|b| match b {
            b'0'..=b'9' => Ok(b - b'0'),
            b'a'..=b'f' => Ok(b - b'a' + 10),
            b'A'..=b'F' => Ok(b - b'A' + 10),
            _ => Err(format!("invalid hex character 0x{b:02x}")),
        })
        .collect::<Result<_, _>>()?;
    if nibbles.len() % 2 != 0 {
        return Err("hex string has odd length".to_string());
    }
    Ok(nibbles
        .chunks_exact(2)
        .map(|pair| (pair[0] << 4) | pair[1])
        .collect())
}
