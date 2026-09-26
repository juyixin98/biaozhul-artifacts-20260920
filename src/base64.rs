//! Minimal standard-alphabet base64 codec, implemented from scratch.
//!
//! Used only for inline blob bytes embedded in JSON build requests.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Decode standard base64 with mandatory padding. Whitespace is rejected.
pub fn decode(input: &str) -> crate::Result<Vec<u8>> {
    let bytes = input.as_bytes();
    if bytes.len() % 4 != 0 {
        return Err(crate::IfixError::Json(
            "base64: length must be a multiple of 4".into(),
        ));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    let mut chunks = bytes.chunks_exact(4);
    let mut count = 0usize;
    for chunk in &mut chunks {
        count += 1;
        let mut vals = [0u8; 4];
        let mut pad = 0;
        for (i, &c) in chunk.iter().enumerate() {
            vals[i] = if c == b'=' {
                pad += 1;
                if pad > 2 || i < 2 {
                    return Err(crate::IfixError::Json("base64: misplaced padding".into()));
                }
                0
            } else {
                if pad > 0 {
                    return Err(crate::IfixError::Json("base64: data after padding".into()));
                }
                decode_char(c)?
            };
        }
        let triple = ((vals[0] as u32) << 18)
            | ((vals[1] as u32) << 12)
            | ((vals[2] as u32) << 6)
            | vals[3] as u32;
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad < 1 {
            out.push(triple as u8);
        }
    }
    if count == 0 && !bytes.is_empty() {
        return Err(crate::IfixError::Json("base64: invalid input".into()));
    }
    Ok(out)
}

fn decode_char(c: u8) -> crate::Result<u8> {
    ALPHABET
        .iter()
        .position(|&a| a == c)
        .map(|p| p as u8)
        .ok_or_else(|| crate::IfixError::Json(format!("base64: invalid character {c:?}")))
}

/// Standard base64 encode with padding (used by JSON responses/tests).
pub fn encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3F) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc4648_vectors() {
        for (plain, enc) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(encode(plain.as_bytes()), enc);
            assert_eq!(String::from_utf8(decode(enc).unwrap()).unwrap(), plain);
        }
    }

    #[test]
    fn rejects_bad_padding() {
        assert!(decode("Z===").is_err());
        assert!(decode("Zg=Z").is_err());
        assert!(decode("Zg").is_err());
        assert!(decode("****").is_err());
    }
}
