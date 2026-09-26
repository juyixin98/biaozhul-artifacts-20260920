//! Self-implemented UTF-8 encoder: Unicode scalar value -> UTF-8 bytes.
//!
//! Rejects surrogate halves (U+D800..=U+DFFF) and values above U+10FFFF.
//! Never produces overlong forms by construction.

use crate::error::{EncodeError, EncodeErrorKind};

/// Append the UTF-8 encoding of `cp` to `out`.
///
/// Returns an error for surrogate halves and out-of-range values;
/// `out` is left untouched in that case.
pub fn encode_codepoint(cp: u32, out: &mut Vec<u8>) -> Result<(), EncodeError> {
    if cp <= 0x7F {
        out.push(cp as u8);
    } else if cp <= 0x7FF {
        out.push(0xC0 | (cp >> 6) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else if (0xD800..=0xDFFF).contains(&cp) {
        return Err(EncodeError {
            kind: EncodeErrorKind::SurrogateCodePoint,
            codepoint: cp,
        });
    } else if cp <= 0xFFFF {
        out.push(0xE0 | (cp >> 12) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else if cp <= 0x10FFFF {
        out.push(0xF0 | (cp >> 18) as u8);
        out.push(0x80 | ((cp >> 12) & 0x3F) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else {
        return Err(EncodeError {
            kind: EncodeErrorKind::CodePointOutOfRange,
            codepoint: cp,
        });
    }
    Ok(())
}

/// Encode a string to UTF-8 bytes using this crate's own encoder.
///
/// The input is already valid UTF-8 (it is a `&str`), so encoding can
/// never fail; this exists so the whole pipeline exercises the same
/// self-implemented code path.
pub fn encode_str(s: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(s.len());
    for c in s.chars() {
        // A char is always a valid scalar value, so this cannot fail.
        let _ = encode_codepoint(c as u32, &mut out);
    }
    out
}
