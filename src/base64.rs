//! 极简 Base64（标准字母表，带填充）——避免为编解码再引入外部依赖。

const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((triple >> 18) & 0x3f) as usize] as char);
        out.push(TABLE[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(TABLE[((triple >> 6) & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(TABLE[(triple & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

pub fn decode(input: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(input.len() / 4 * 3);
    let bytes: Vec<u8> = input.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    let mut chunk = [0u8; 4];
    let mut len = 0usize;
    let mut pad = 0usize;
    for b in bytes {
        if b == b'=' {
            pad += 1;
            chunk[len] = 64;
        } else {
            let val = match b {
                b'A'..=b'Z' => b - b'A',
                b'a'..=b'z' => b - b'a' + 26,
                b'0'..=b'9' => b - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                _ => return None,
            };
            chunk[len] = val;
        }
        len += 1;
        if len == 4 {
            if pad > 2 {
                return None;
            }
            let v = ((chunk[0] & 0x3f) as u32) << 18
                | ((chunk[1] & 0x3f) as u32) << 12
                | ((chunk[2] & 0x3f) as u32) << 6
                | (chunk[3] & 0x3f) as u32;
            out.push((v >> 16) as u8);
            if chunk[2] != 64 {
                out.push((v >> 8) as u8);
            }
            if chunk[3] != 64 {
                out.push(v as u8);
            }
            len = 0;
        }
    }
    if len != 0 {
        return None; // 非 4 整数倍长度
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        for case in [
            &b""[..],
            &b"f"[..],
            &b"fo"[..],
            &b"foo"[..],
            &b"foob"[..],
            &b"hello world"[..],
            &[0u8, 1, 2, 3, 250, 255],
        ] {
            let s = encode(case);
            assert_eq!(&decode(&s).unwrap(), case);
        }
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(decode("Zm9v").unwrap(), b"foo");
        assert!(decode("!!!!").is_none());
    }
}
