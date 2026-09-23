//! Standard base64 (RFC 4648) encode/decode, dependency-free.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

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

#[derive(Debug, PartialEq, Eq)]
pub struct DecodeError;

pub fn decode(input: &str) -> Result<Vec<u8>, DecodeError> {
    fn val(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }

    let bytes: Vec<u8> = input.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if !bytes.len().is_multiple_of(4) {
        return Err(DecodeError);
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.as_chunks::<4>().0 {
        let mut vals = [0u8; 4];
        let mut pad = 0;
        for i in 0..4 {
            if chunk[i] == b'=' {
                vals[i] = 0;
                pad += 1;
            } else {
                vals[i] = val(chunk[i]).ok_or(DecodeError)?;
            }
        }
        if pad > 2 {
            return Err(DecodeError);
        }
        // Padding may only occur as a contiguous run at the tail.
        if pad > 0
            && (!chunk[..4 - pad].iter().all(|&c| c != b'=')
                || !chunk[4 - pad..].iter().all(|&c| c == b'='))
        {
            return Err(DecodeError);
        }
        let triple = ((vals[0] as u32) << 18)
            | ((vals[1] as u32) << 12)
            | ((vals[2] as u32) << 6)
            | (vals[3] as u32);
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad < 1 {
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
        for len in 0..=30 {
            let data: Vec<u8> = (0..len).map(|i| (i * 37 + 5) as u8).collect();
            let s = encode(&data);
            assert_eq!(decode(&s).unwrap(), data);
        }
    }

    #[test]
    fn rejects_bad_input() {
        assert!(decode("Z").is_err());
        assert!(decode("Zg=").is_err());
        assert!(decode("!!!!").is_err());
    }
}
