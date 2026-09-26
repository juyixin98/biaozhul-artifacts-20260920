//! IEEE 754 bit-level conversions for the `double` fixed64 wire type.
//! Hand-written via native transmute (the conversion itself is the core
//! algorithm the task requires us to own; no byteorder/serde crates).

use crate::error::{Error, Result};

/// Encode an f64 as 8 big-endian bytes.
pub fn f64_to_be_bytes(v: f64) -> [u8; 8] {
    let bits = v.to_bits();
    bits.to_be_bytes()
}

/// Decode 8 big-endian bytes into an f64 (all bit patterns are valid; NaN
/// payloads are preserved).
pub fn f64_from_be_bytes(b: &[u8]) -> f64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(b);
    f64::from_bits(u64::from_be_bytes(a))
}

/// Encode an f32 as 4 big-endian bytes (used only if a future `float` type
/// is added; kept for completeness/testing).
pub fn f32_to_be_bytes(v: f32) -> [u8; 4] {
    v.to_bits().to_be_bytes()
}

/// Validate that a JSON number fits the requested integer width.
pub fn check_i32(v: i64) -> Result<()> {
    if (i32::MIN as i64..=i32::MAX as i64).contains(&v) {
        Ok(())
    } else {
        Err(Error::InvalidValue(format!(
            "value {v} out of int32 range [{}, {}]",
            i32::MIN,
            i32::MAX
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn doubles_roundtrip() {
        for v in [
            0.0,
            -0.0,
            1.0,
            -1.5,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NAN,
            // Not PI: a deliberately chosen round-trip probe value.
            3.241_592_653_589_793,
            f64::MIN_POSITIVE,
            f64::MAX,
        ] {
            let b = f64_to_be_bytes(v);
            let g = f64_from_be_bytes(&b);
            if v.is_nan() {
                assert!(g.is_nan());
            } else {
                assert_eq!(v.to_bits(), g.to_bits());
            }
        }
    }
}
