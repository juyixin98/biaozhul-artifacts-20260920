//! Zigzag mapping between signed deltas and unsigned integers.
//!
//! Deltas are computed in 128 bits so that `i64::MAX - i64::MIN` cannot
//! overflow; the zigzag image of any such delta fits in 66 bits, which is
//! why even a 64-bit block can need escape exceptions.

/// Map a signed delta to an unsigned value: 0,-1,1,-2,2 -> 0,1,2,3,4.
pub fn zigzag_encode(d: i128) -> u128 {
    // |d| < 2^64 for any delta of two i64 values, so the shift is safe.
    ((d << 1) ^ (d >> 127)) as u128
}

/// Inverse of [`zigzag_encode`].
pub fn zigzag_decode(zz: u128) -> i128 {
    ((zz >> 1) as i128) ^ -((zz & 1) as i128)
}

/// Number of bits needed to represent `z` (0 for zero).
pub fn bit_len(z: u128) -> u32 {
    128 - z.leading_zeros()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_extremes() {
        for d in [
            0i128,
            -1,
            1,
            i64::MIN as i128,
            i64::MAX as i128,
            i64::MAX as i128 - i64::MIN as i128,
            i64::MIN as i128 - i64::MAX as i128,
        ] {
            assert_eq!(zigzag_decode(zigzag_encode(d)), d);
        }
    }

    #[test]
    fn bit_len_edges() {
        assert_eq!(bit_len(0), 0);
        assert_eq!(bit_len(1), 1);
        assert_eq!(bit_len(u64::MAX as u128), 64);
        assert_eq!(bit_len(u64::MAX as u128 + 1), 65);
    }
}
