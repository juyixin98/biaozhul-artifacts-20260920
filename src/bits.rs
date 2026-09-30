//! Fixed-universe packed bitmaps over `u64` words.
//!
//! Invariants enforced by *every* constructor / combinator:
//! * `len` is the universe size in bits; words beyond `len` do not exist.
//! * The unused high bits of the last word ("tail bits") are always zero.
//!
//! The tail invariant matters because this engine repeatedly combines bitmaps
//! with bitwise OR/AND/AND-NOT: one dirty tail bit is exactly the kind of
//! boundary bug where "normal inputs look right but edge inputs silently
//! miscompute" (e.g. a 65-row universe reporting a phantom 66th match after
//! `NOT` + `OR`). Use [`WordBitmap::validate`] in tests/debug assertions.

use crate::error::{ErrorKind, IndexError, Result};

/// Number of bits stored per machine word.
pub const WORD_BITS: usize = u64::BITS as usize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WordBitmap {
    len: usize,
    words: Vec<u64>,
}

/// Mask of the valid (low) bits of the last word for a universe of `len` bits.
///
/// * `len == 0`            -> `0` (no valid bits at all)
/// * `len % 64 == 0`       -> `u64::MAX` (the last word is fully used)
/// * otherwise             -> low `len % 64` bits set
fn tail_mask(len: usize) -> u64 {
    let rem = len % WORD_BITS;
    if rem == 0 {
        u64::MAX
    } else {
        (1u64 << rem) - 1
    }
}

fn words_for(len: usize) -> usize {
    len.div_ceil(WORD_BITS)
}

impl WordBitmap {
    /// All-zero bitmap over a universe of `len` bits.
    pub fn zeros(len: usize) -> Self {
        Self { len, words: vec![0u64; words_for(len)] }
    }

    /// All-one bitmap over a universe of `len` bits (tail bits cleared).
    pub fn ones(len: usize) -> Self {
        let mut b = Self { len, words: vec![u64::MAX; words_for(len)] };
        b.clear_tail();
        b
    }

    /// Build from raw words. Tail bits are forced to zero defensively.
    pub fn from_words(len: usize, words: Vec<u64>) -> Result<Self> {
        if words.len() != words_for(len) {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                format!("from_words: {} words supplied for {} bits (expected {})", words.len(), len, words_for(len)),
            ));
        }
        let mut b = Self { len, words };
        b.clear_tail();
        Ok(b)
    }

    /// Build by setting every index in `indices`.
    pub fn from_indices(len: usize, indices: impl IntoIterator<Item = usize>) -> Result<Self> {
        let mut b = Self::zeros(len);
        for i in indices {
            b.set(i)?;
        }
        // `set` already bounds-checks; tail stays clean because `set` can only
        // address valid positions, but clearing again is a cheap invariant belt.
        b.clear_tail();
        Ok(b)
    }

    /// Build from one bool per row.
    pub fn from_bools(len: usize, values: impl IntoIterator<Item = bool>) -> Result<Self> {
        let mut b = Self::zeros(len);
        for (i, v) in values.into_iter().enumerate() {
            if i >= len {
                return Err(IndexError::new(
                    ErrorKind::InvalidBitmap,
                    format!("from_bools: iterator produced row {} beyond len {}", i, len),
                ));
            }
            if v {
                // In-bounds by the check above.
                b.words[i / WORD_BITS] |= 1u64 << (i % WORD_BITS);
            }
        }
        b.clear_tail();
        Ok(b)
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn words(&self) -> &[u64] {
        &self.words
    }

    #[inline]
    pub fn get(&self, i: usize) -> bool {
        assert!(i < self.len, "bit index {i} out of bounds for len {}", self.len);
        (self.words[i / WORD_BITS] >> (i % WORD_BITS)) & 1 == 1
    }

    /// Set a single bit (in place during index construction).
    pub fn set(&mut self, i: usize) -> Result<()> {
        if i >= self.len {
            return Err(IndexError::new(
                ErrorKind::RowIndexOutOfBounds,
                format!("set: row {i} out of bounds for universe of {}", self.len),
            ));
        }
        self.words[i / WORD_BITS] |= 1u64 << (i % WORD_BITS);
        Ok(())
    }

    /// Population count.
    pub fn count(&self) -> usize {
        self.words.iter().map(|w| w.count_ones() as usize).sum()
    }

    /// Force the unused high bits of the last word to zero.
    pub fn clear_tail(&mut self) {
        if self.len == 0 {
            return;
        }
        if let Some(last) = self.words.last_mut() {
            *last &= tail_mask(self.len);
        }
    }

    /// Structural validation: word count and tail cleanliness.
    pub fn validate(&self) -> Result<()> {
        if self.words.len() != words_for(self.len) {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                format!("validate: word count {} != {}", self.words.len(), words_for(self.len)),
            ));
        }
        if self.len > 0 {
            if let Some(&last) = self.words.last() {
                let dirty = last & !tail_mask(self.len);
                if dirty != 0 {
                    return Err(IndexError::new(
                        ErrorKind::InvalidBitmap,
                        format!("validate: tail contains invalid bits {dirty:#018x} for len {}", self.len),
                    ));
                }
            }
        }
        Ok(())
    }

    fn check_same_len(&self, other: &Self, op: &str) -> Result<()> {
        if self.len != other.len {
            return Err(IndexError::new(
                ErrorKind::LengthMismatch,
                format!("{op}: universe lengths {} and {} differ", self.len, other.len),
            ));
        }
        Ok(())
    }

    fn binop(&self, other: &Self, op: &str, f: impl Fn(u64, u64) -> u64) -> Result<Self> {
        self.check_same_len(other, op)?;
        let words: Vec<u64> = self.words.iter().zip(&other.words).map(|(a, b)| f(*a, *b)).collect();
        let mut out = Self { len: self.len, words };
        out.clear_tail();
        out.validate()?;
        Ok(out)
    }

    pub fn intersect(&self, other: &Self) -> Result<Self> {
        self.binop(other, "intersect", |a, b| a & b)
    }

    pub fn union(&self, other: &Self) -> Result<Self> {
        self.binop(other, "union", |a, b| a | b)
    }

    /// Rows in `self` that are not in `other` (`self AND NOT other`).
    pub fn and_not(&self, other: &Self) -> Result<Self> {
        // `a & !b`: the machine complement of `b` may light tail bits, but the
        // AND with the tail-clean `a` removes them; `binop` clears the tail once
        // more and validates, so the universe boundary can never leak.
        self.binop(other, "and_not", |a, b| a & !b)
    }

    pub fn is_disjoint(&self, other: &Self) -> Result<bool> {
        self.check_same_len(other, "is_disjoint")?;
        Ok(self.words.iter().zip(&other.words).all(|(a, b)| a & b == 0))
    }

    /// Indices of all set bits, ascending.
    pub fn set_indices(&self) -> Vec<usize> {
        let mut out = Vec::with_capacity(self.count());
        for (w, word) in self.words.iter().enumerate() {
            let mut acc = *word;
            while acc != 0 {
                let lsb = acc.trailing_zeros() as usize;
                out.push(w * WORD_BITS + lsb);
                acc &= acc - 1;
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_bits_are_zero_for_non_word_lengths() {
        for len in 1..=200usize {
            let ones = WordBitmap::ones(len);
            ones.validate().unwrap();
            assert_eq!(ones.count(), len, "ones({len}) must contain exactly {len} rows, no phantom tail rows");
            // The raw last word must have no bits beyond `len`.
            if let Some(&last) = ones.words().last() {
                let rem = len % WORD_BITS;
                if rem != 0 {
                    assert_eq!(last, (1u64 << rem) - 1, "len={len}");
                } else {
                    assert_eq!(last, u64::MAX, "len={len}");
                }
            }
        }
    }

    #[test]
    fn complement_stays_inside_universe() {
        for len in [0usize, 1, 63, 64, 65, 127, 128, 129, 130] {
            let all = WordBitmap::ones(len);
            let none = WordBitmap::zeros(len);
            assert_eq!(all.and_not(&none).unwrap().count(), len);
            assert_eq!(none.and_not(&all).unwrap().count(), 0);
            let back = all.and_not(&all).unwrap();
            assert_eq!(back.count(), 0);
            back.validate().unwrap();
        }
    }

    #[test]
    fn set_beyond_len_is_rejected() {
        let mut b = WordBitmap::zeros(65);
        let err = b.set(65).unwrap_err();
        assert_eq!(err.kind, ErrorKind::RowIndexOutOfBounds);
        let err = b.set(100).unwrap_err();
        assert_eq!(err.kind, ErrorKind::RowIndexOutOfBounds);
    }

    #[test]
    fn length_mismatch_is_rejected() {
        let a = WordBitmap::zeros(64);
        let b = WordBitmap::zeros(65);
        assert_eq!(a.intersect(&b).unwrap_err().kind, ErrorKind::LengthMismatch);
        assert_eq!(a.union(&b).unwrap_err().kind, ErrorKind::LengthMismatch);
        assert_eq!(a.and_not(&b).unwrap_err().kind, ErrorKind::LengthMismatch);
    }
}
