//! Fixed-length, LSB-first bitmap over an explicit finite universe `[0, len)`.
//!
//! # Invariants
//!
//! * `len` is the universe size; bit positions `len..word_len*64` do not exist.
//! * The invalid "tail" bits of the last machine word are ALWAYS zero, after
//!   every constructor and every operation. This is what lets set algebra be
//!   implemented word-wise while keeping `NOT` honest (see [`Bitmap::complement`]).

use std::ops::BitAnd;

/// Mask of the valid bits in the last word for a universe of `len` bits.
#[inline]
fn tail_mask(len: usize) -> u64 {
    let rem = len % 64;
    if rem == 0 {
        u64::MAX
    } else {
        (1u64 << rem) - 1
    }
}

/// Number of 64-bit machine words needed for `len` bits.
#[inline]
pub fn words_for(len: usize) -> usize {
    len.div_ceil(64)
}

#[derive(Clone, PartialEq, Eq)]
pub struct Bitmap {
    bits: Vec<u64>,
    len: usize,
}

impl std::fmt::Debug for Bitmap {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Bitmap")
            .field("len", &self.len)
            .field("ones", &self.count_ones())
            .finish()
    }
}

impl Bitmap {
    /// Empty set over a universe of `len` positions.
    pub fn new(len: usize) -> Self {
        Self {
            bits: vec![0u64; words_for(len)],
            len,
        }
    }

    /// Full set over a universe of `len` positions (tail bits cleared).
    pub fn all(len: usize) -> Self {
        let mut b = Self {
            bits: vec![u64::MAX; words_for(len)],
            len,
        };
        b.clear_tail();
        b
    }

    /// Build from raw machine words. `words.len()` must equal `words_for(len)`.
    /// Any invalid tail bits in the last word are cleared (never silently kept).
    pub fn from_words(len: usize, words: Vec<u64>) -> Result<Self, BitmapError> {
        if words.len() != words_for(len) {
            return Err(BitmapError::WordCount {
                len,
                expected: words_for(len),
                got: words.len(),
            });
        }
        let mut b = Self { bits: words, len };
        b.clear_tail();
        Ok(b)
    }

    /// Build from a boolean predicate evaluated for every universe position.
    pub fn from_predicate(len: usize, mut pred: impl FnMut(usize) -> bool) -> Self {
        let mut b = Self::new(len);
        for i in 0..len {
            if pred(i) {
                // SAFETY-of-meaning: i < len guaranteed.
                b.set(i);
            }
        }
        b
    }

    /// Build from a sorted-or-unsorted iterable of set positions.
    pub fn from_indices(len: usize, indices: impl IntoIterator<Item = usize>) -> Self {
        let mut b = Self::new(len);
        for i in indices {
            assert!(i < len, "bit index {i} out of universe [0,{len})");
            b.set(i);
        }
        b
    }

    // `is_empty` is deliberately not provided under that name: for a bitmap
    // "empty" is ambiguous between an empty universe (`is_empty_len`) and an
    // empty set (`is_empty_set`), so both get explicit names instead.
    #[allow(clippy::len_without_is_empty)]
    #[inline]
    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty_len(&self) -> bool {
        self.len == 0
    }

    #[inline]
    fn clear_tail(&mut self) {
        if self.len == 0 {
            return;
        }
        let last = self.bits.len() - 1;
        self.bits[last] &= tail_mask(self.len);
    }

    /// Debug-only structural check used by tests and by higher layers.
    pub fn debug_assert_valid(&self) {
        if self.len == 0 {
            debug_assert!(self.bits.is_empty());
            return;
        }
        debug_assert_eq!(self.bits.len(), words_for(self.len));
        let last = self.bits.len() - 1;
        debug_assert_eq!(self.bits[last] & !tail_mask(self.len), 0, "tail bits must be zero");
    }

    #[inline]
    pub fn get(&self, idx: usize) -> bool {
        assert!(idx < self.len, "bit index {idx} out of universe [0,{})", self.len);
        (self.bits[idx / 64] >> (idx % 64)) & 1 == 1
    }

    #[inline]
    pub fn set(&mut self, idx: usize) {
        assert!(idx < self.len, "bit index {idx} out of universe [0,{})", self.len);
        self.bits[idx / 64] |= 1u64 << (idx % 64);
    }

    #[inline]
    pub fn unset(&mut self, idx: usize) {
        assert!(idx < self.len, "bit index {idx} out of universe [0,{})", self.len);
        self.bits[idx / 64] &= !(1u64 << (idx % 64));
    }

    /// Number of set bits.
    pub fn count_ones(&self) -> usize {
        self.bits.iter().map(|w| w.count_ones() as usize).sum()
    }

    /// Number of unset bits *within the universe* (tail bits never counted).
    pub fn count_zeros(&self) -> usize {
        self.len - self.count_ones()
    }

    pub fn is_empty_set(&self) -> bool {
        self.bits.iter().all(|w| *w == 0)
    }

    pub fn is_full(&self) -> bool {
        self.count_ones() == self.len
    }

    /// Raw machine words (tail bits guaranteed zero).
    pub fn words(&self) -> &[u64] {
        &self.bits
    }

    /// Iterator over set positions, ascending.
    pub fn iter_ones(&self) -> impl Iterator<Item = usize> + '_ {
        self.bits.iter().enumerate().flat_map(|(wi, &word)| {
            let base = wi * 64;
            let mut w = word;
            std::iter::from_fn(move || {
                if w == 0 {
                    return None;
                }
                let tz = w.trailing_zeros() as usize;
                w &= w - 1;
                Some(base + tz)
            })
        })
    }

    fn check_same_universe(&self, other: &Self, op: &str) -> Result<(), BitmapError> {
        if self.len != other.len {
            return Err(BitmapError::UniverseMismatch {
                op: op.to_string(),
                left: self.len,
                right: other.len,
            });
        }
        Ok(())
    }

    /// Set union: `self ∪ other`.
    pub fn union(&self, other: &Self) -> Result<Self, BitmapError> {
        self.check_same_universe(other, "union")?;
        let bits: Vec<u64> = self
            .bits
            .iter()
            .zip(&other.bits)
            .map(|(a, b)| a | b)
            .collect();
        // OR of two tail-clean bitmaps cannot set tail bits; mask anyway as defence in depth.
        Ok(Self::from_words(self.len, bits).expect("word count matched"))
    }

    /// Set intersection: `self ∩ other`.
    pub fn intersection(&self, other: &Self) -> Result<Self, BitmapError> {
        self.check_same_universe(other, "intersection")?;
        let bits: Vec<u64> = self
            .bits
            .iter()
            .zip(&other.bits)
            .map(|(a, b)| a & b)
            .collect();
        Ok(Self::from_words(self.len, bits).expect("word count matched"))
    }

    /// Relative complement: `self ∖ other` (`self AND NOT other`).
    ///
    /// This is computed word-wise as `a & !b`, which is safe precisely because
    /// `self` is tail-clean: bits beyond the universe can never become 1.
    pub fn difference(&self, other: &Self) -> Result<Self, BitmapError> {
        self.check_same_universe(other, "difference")?;
        let bits: Vec<u64> = self
            .bits
            .iter()
            .zip(&other.bits)
            .map(|(a, b)| a & !b)
            .collect();
        Ok(Self::from_words(self.len, bits).expect("word count matched"))
    }

    /// Absolute complement **within the explicit universe**: `U ∖ self`.
    ///
    /// # Why this is not `!word`
    ///
    /// Naively negating each machine word (`!self.bits[i]`) sets every bit in
    /// the unused tail of the last word to 1, and — more importantly — would
    /// only be meaningful relative to the machine-word boundary rather than the
    /// document universe. SQL `NOT` must be the universe-relative complement,
    /// so the last word is explicitly masked with [`tail_mask`].
    pub fn complement(&self) -> Self {
        let mut bits: Vec<u64> = self.bits.iter().map(|w| !w).collect();
        if self.len > 0 {
            let last = bits.len() - 1;
            bits[last] &= tail_mask(self.len);
        }
        Self { bits, len: self.len }
    }

    pub fn is_disjoint(&self, other: &Self) -> Result<bool, BitmapError> {
        self.check_same_universe(other, "is_disjoint")?;
        Ok(self
            .bits
            .iter()
            .zip(&other.bits)
            .all(|(a, b)| (a & b) == 0))
    }

    pub fn is_subset_of(&self, other: &Self) -> Result<bool, BitmapError> {
        self.check_same_universe(other, "is_subset_of")?;
        Ok(self
            .bits
            .iter()
            .zip(&other.bits)
            .all(|(a, b)| (a & !b) == 0))
    }
}

impl BitAnd<&Bitmap> for &Bitmap {
    type Output = Result<Bitmap, BitmapError>;
    fn bitand(self, rhs: &Bitmap) -> Self::Output {
        self.intersection(rhs)
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum BitmapError {
    #[error("universe size mismatch in {op}: left={left}, right={right}")]
    UniverseMismatch {
        op: String,
        left: usize,
        right: usize,
    },
    #[error("word count mismatch for universe {len}: expected {expected}, got {got}")]
    WordCount {
        len: usize,
        expected: usize,
        got: usize,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_bits_cleared_on_all_and_complement() {
        for len in [0usize, 1, 7, 8, 9, 63, 64, 65, 127, 128, 129, 200] {
            let all = Bitmap::all(len);
            all.debug_assert_valid();
            assert_eq!(all.count_ones(), len, "len={len}");
            let none = all.complement();
            none.debug_assert_valid();
            assert_eq!(none.count_ones(), 0, "complement of U empty, len={len}");
            assert_eq!(none.complement().count_ones(), len, "double complement, len={len}");
        }
    }

    #[test]
    fn from_words_strips_dirty_tail() {
        // Universe of 3 bits stored in one word; high bits are garbage.
        let b = Bitmap::from_words(3, vec![0xFFFF_FFFF_FFFF_FFFF]).unwrap();
        assert_eq!(b.count_ones(), 3);
        assert_eq!(b.words()[0], 0b0111);
        // Universe exactly multiple of 64 keeps all bits.
        let b64 = Bitmap::from_words(64, vec![u64::MAX]).unwrap();
        assert_eq!(b64.count_ones(), 64);
    }

    #[test]
    fn complement_is_universe_relative_not_wordwise() {
        let mut b = Bitmap::new(5);
        b.set(0);
        b.set(2);
        let c = b.complement();
        // Only positions 1,3,4 — never 5..64 (the bug word-wise NOT would produce).
        assert_eq!(c.count_ones(), 3);
        assert!(c.get(1) && c.get(3) && c.get(4));
        assert_eq!(c.words()[0], 0b11010);
        assert_eq!(c.words()[0], 0b00101 ^ 0b11111); // U XOR {0,2}
    }

    #[test]
    fn set_algebra_partition_law() {
        let len = 70; // spans two words with a dirty tail
        let a = Bitmap::from_indices(len, [0, 3, 63, 64, 69]);
        let b = a.complement();
        assert!(a.is_disjoint(&b).unwrap());
        assert_eq!(a.union(&b).unwrap().count_ones(), len);
        assert_eq!(a.difference(&b).unwrap(), a);
    }

    #[test]
    fn mismatched_universes_are_errors() {
        let a = Bitmap::new(64);
        let b = Bitmap::new(65);
        assert_eq!(
            a.union(&b).err(),
            Some(BitmapError::UniverseMismatch {
                op: "union".into(),
                left: 64,
                right: 65
            })
        );
    }
}
