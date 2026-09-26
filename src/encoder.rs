//! UTF-8 encoder (code point -> bytes), implemented from scratch.
//!
//! Only Unicode scalar values are accepted: surrogate halves
//! (U+D800..=U+DFFF) and values above U+10FFFF are rejected, and the
//! shortest possible encoding is always produced (so decoding the output
//! can never trip the decoder's overlong-encoding check).

use crate::error::EncodeError;

/// Encode one Unicode scalar value as UTF-8.
///
/// Returns the byte buffer and the number of valid bytes in it (1..=4).
pub fn encode_scalar(cp: u32) -> Result<([u8; 4], usize), EncodeError> {
    if (0xD800..=0xDFFF).contains(&cp) {
        return Err(EncodeError::Surrogate(cp));
    }
    if cp > 0x10FFFF {
        return Err(EncodeError::OutOfRange(cp));
    }
    let mut buf = [0u8; 4];
    let len = if cp < 0x80 {
        buf[0] = cp as u8;
        1
    } else if cp < 0x800 {
        buf[0] = 0xC0 | (cp >> 6) as u8;
        buf[1] = 0x80 | (cp & 0x3F) as u8;
        2
    } else if cp < 0x10000 {
        buf[0] = 0xE0 | (cp >> 12) as u8;
        buf[1] = 0x80 | ((cp >> 6) & 0x3F) as u8;
        buf[2] = 0x80 | (cp & 0x3F) as u8;
        3
    } else {
        buf[0] = 0xF0 | (cp >> 18) as u8;
        buf[1] = 0x80 | ((cp >> 12) & 0x3F) as u8;
        buf[2] = 0x80 | ((cp >> 6) & 0x3F) as u8;
        buf[3] = 0x80 | (cp & 0x3F) as u8;
        4
    };
    Ok((buf, len))
}

/// Encode a slice of code points into a freshly allocated byte vector.
pub fn encode_all(codepoints: &[u32]) -> Result<Vec<u8>, EncodeError> {
    let mut out = Vec::with_capacity(codepoints.len());
    for &cp in codepoints {
        let (buf, len) = encode_scalar(cp)?;
        out.extend_from_slice(&buf[..len]);
    }
    Ok(out)
}
