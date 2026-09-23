//! Minimal checked 256-bit unsigned integer arithmetic.
//!
//! Constant-product swaps multiply two 128-bit reserves/inputs, which can
//! overflow `u128`. EVM AMMs use `uint256` intermediates and revert on
//! overflow, so we mirror that exactly: a small auditable U256 with checked
//! operations. Every operation returns `None` on overflow or division by
//! zero — callers turn that into an explicit `OVERFLOW` rejection, never a
//! silent wrap.

use std::cmp::Ordering;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct U256 {
    hi: u128,
    lo: u128,
}

impl U256 {
    pub const ZERO: U256 = U256 { hi: 0, lo: 0 };

    #[inline]
    pub fn from_u128(v: u128) -> Self {
        U256 { hi: 0, lo: v }
    }

    /// Exact 128-bit × 128-bit → 256-bit multiply. Always infallible.
    pub fn mul_u128s(a: u128, b: u128) -> Self {
        // 64-bit limb schoolbook multiplication; every partial product fits u128.
        let (a0, a1) = (a as u64 as u128, a >> 64);
        let (b0, b1) = (b as u64 as u128, b >> 64);

        let p00 = a0 * b0;
        let p01 = a0 * b1;
        let p10 = a1 * b0;
        let p11 = a1 * b1;

        let r0 = p00 as u64 as u128;
        let carry = p00 >> 64;

        let mid = (p01 as u64 as u128) + (p10 as u64 as u128) + carry;
        let r1 = mid as u64 as u128;

        let high = (p01 >> 64) + (p10 >> 64) + (p11 as u64 as u128) + (mid >> 64);
        let r2 = high as u64 as u128;
        // limb 3 also receives the upper half of p11 (a1*b1 sits at bit 128).
        let r3 = (p11 >> 64) + (high >> 64); // <= ~2^64, no carry past bit 256

        U256 {
            lo: r0 | (r1 << 64),
            hi: r2 | (r3 << 64),
        }
    }

    pub fn checked_add(self, rhs: Self) -> Option<Self> {
        let (lo, c1) = self.lo.overflowing_add(rhs.lo);
        let hi = self
            .hi
            .checked_add(rhs.hi)?
            .checked_add(c1 as u128)?;
        Some(U256 { hi, lo })
    }

    pub fn checked_sub(self, rhs: Self) -> Option<Self> {
        if self < rhs {
            return None;
        }
        let (lo, borrow) = self.lo.overflowing_sub(rhs.lo);
        let hi = self.hi - rhs.hi - borrow as u128;
        Some(U256 { hi, lo })
    }

    /// Multiply by a 128-bit factor; `None` if the result exceeds 256 bits.
    pub fn checked_mul_u128(self, b: u128) -> Option<Self> {
        // self * b = (lo*b) + (hi*b) << 128
        let p_lo = Self::mul_u128s(self.lo, b);
        let p_hi = Self::mul_u128s(self.hi, b);
        // p_hi occupies bits 128..=383; its high half must be zero.
        if p_hi.hi != 0 {
            return None;
        }
        let shifted = U256 {
            hi: p_hi.lo.checked_add(p_lo.hi)?,
            lo: p_lo.lo,
        };
        Some(shifted)
    }

    /// Bitwise long division. `None` only when `rhs == 0` or an internal
    /// shift overflows (cannot happen for in-range values).
    pub fn checked_div(self, rhs: Self) -> Option<Self> {
        if rhs == U256::ZERO {
            return None;
        }
        if self < rhs {
            return Some(U256::ZERO);
        }
        let mut rem = U256::ZERO;
        let mut quot = U256::ZERO;
        for i in (0..256).rev() {
            // rem = (rem << 1) | bit_i(self)
            let bit = (self.bit_word(i) >> (i % 128)) & 1;
            rem = rem.shl1_with(bit)?;
            if rem >= rhs {
                rem = rem.checked_sub(rhs)?;
                quot = quot.shl1_with(1)?;
            } else {
                quot = quot.shl1_with(0)?;
            }
        }
        Some(quot)
    }

    #[inline]
    fn bit_word(&self, i: u32) -> u128 {
        if i >= 128 {
            self.hi
        } else {
            self.lo
        }
    }

    #[inline]
    fn shl1_with(self, bit: u128) -> Option<Self> {
        if self.hi >> 127 != 0 {
            return None;
        }
        Some(U256 {
            hi: (self.hi << 1) | (self.lo >> 127),
            lo: (self.lo << 1) | (bit & 1),
        })
    }

    pub fn to_u128(self) -> Option<u128> {
        if self.hi == 0 {
            Some(self.lo)
        } else {
            None
        }
    }

    pub fn is_zero(&self) -> bool {
        *self == U256::ZERO
    }
}

impl PartialOrd for U256 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for U256 {
    fn cmp(&self, other: &Self) -> Ordering {
        self.hi
            .cmp(&other.hi)
            .then_with(|| self.lo.cmp(&other.lo))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_products() {
        assert_eq!(U256::mul_u128s(0, u128::MAX), U256::ZERO);
        // (2^128 - 1)^2 = 2^256 - 2^129 + 1
        let p = U256::mul_u128s(u128::MAX, u128::MAX);
        assert_eq!(p.hi, u128::MAX - 1);
        assert_eq!(p.lo, 1);
        // 2^64 * 2^64 = 2^128
        let p = U256::mul_u128s(1u128 << 64, 1u128 << 64);
        assert_eq!((p.hi, p.lo), (1, 0));
    }

    #[test]
    fn add_overflow() {
        let max = U256 {
            hi: u128::MAX,
            lo: u128::MAX,
        };
        assert!(max.checked_add(U256::from_u128(1)).is_none());
        let v = U256 {
            hi: u128::MAX,
            lo: u128::MAX - 1,
        };
        let r = v.checked_add(U256::from_u128(1)).unwrap();
        assert_eq!(r, max);
    }

    #[test]
    fn sub_semantics() {
        assert!(U256::from_u128(3).checked_sub(U256::from_u128(4)).is_none());
        assert_eq!(
            U256::from_u128(10).checked_sub(U256::from_u128(4)).unwrap(),
            U256::from_u128(6)
        );
    }

    #[test]
    fn mul_u128_overflow() {
        // 2^200 * 2^100 overflows 256 bits.
        let v = U256 {
            hi: 1u128 << 72, // 2^200
            lo: 0,
        };
        assert!(v.checked_mul_u128(1u128 << 100).is_none());
        assert!(v.checked_mul_u128(1u128 << 56).is_none()); // 2^200 * 2^56 = 2^256
        // 2^200 * 2^55 = 2^255 fits.
        assert!(v.checked_mul_u128(1u128 << 55).is_some());
    }

    #[test]
    fn division_examples() {
        assert_eq!(
            U256::from_u128(100).checked_div(U256::from_u128(7)).unwrap(),
            U256::from_u128(14) // floor
        );
        assert!(U256::from_u128(1).checked_div(U256::ZERO).is_none());
        let big = U256::mul_u128s(u128::MAX, 1000);
        let q = big.checked_div(U256::from_u128(1000)).unwrap();
        assert_eq!(q.to_u128(), Some(u128::MAX));
        // quotient spanning both limbs
        let num = U256 {
            hi: 5,
            lo: 0,
        }; // 5 * 2^128
        let q = num
            .checked_div(U256::from_u128(2))
            .unwrap();
        assert_eq!(q, U256 { hi: 2, lo: 1u128 << 127 });
    }

    #[test]
    fn ordering_works() {
        assert!(U256 { hi: 1, lo: 0 } > U256 { hi: 0, lo: u128::MAX });
    }
}
