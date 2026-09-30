//! A minimal fixed-size bitset used for candidate bitmaps.
//!
//! Bit positions are the **compact sorted positions** produced by
//! [`crate::operator::sort::SortedColumn`] (NULL/NaN rows are absent).

#[derive(Debug, Clone)]
pub struct BitSet {
    words: Vec<u64>,
    len: usize,
}

impl BitSet {
    /// All-zero bitset of capacity `len`.
    #[must_use]
    pub fn zeros(len: usize) -> Self {
        let words = len.div_ceil(64);
        BitSet { words: vec![0; words], len }
    }

    /// All-one bitset; trailing padding bits past `len` stay zero.
    #[must_use]
    pub fn ones(len: usize) -> Self {
        let mut s = Self::zeros(len);
        for w in &mut s.words {
            *w = u64::MAX;
        }
        s.clear_padding();
        s
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.len
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.words.iter().all(|w| *w == 0)
    }

    pub fn set(&mut self, pos: usize) {
        debug_assert!(pos < self.len, "bit position out of range");
        self.words[pos >> 6] |= 1u64 << (pos & 63);
    }

    #[cfg(test)]
    pub fn clear(&mut self, pos: usize) {
        debug_assert!(pos < self.len, "bit position out of range");
        self.words[pos >> 6] &= !(1u64 << (pos & 63));
    }

    #[must_use]
    pub fn test(&self, pos: usize) -> bool {
        debug_assert!(pos < self.len, "bit position out of range");
        self.words[pos >> 6] & (1u64 << (pos & 63)) != 0
    }

    /// Raw 64-bit word (used for word-level scan accounting).
    #[must_use]
    pub fn word(&self, wi: usize) -> u64 {
        self.words[wi]
    }

    #[cfg(test)]
    #[must_use]
    pub fn count_ones(&self) -> usize {
        self.words.iter().map(|w| w.count_ones() as usize).sum()
    }

    /// Iterator over set bit positions in ascending order.
    #[cfg(test)]
    pub fn iter_ones(&self) -> impl Iterator<Item = usize> + '_ {
        self.words.iter().enumerate().flat_map(|(wi, &word)| {
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

    fn clear_padding(&mut self) {
        let rem = self.len % 64;
        if rem != 0 {
            if let Some(last) = self.words.last_mut() {
                *last &= (1u64 << rem) - 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_clear_iter_count() {
        let mut b = BitSet::zeros(130);
        assert_eq!(b.iter_ones().count(), 0);
        for p in [0usize, 63, 64, 129] {
            b.set(p);
        }
        assert_eq!(b.count_ones(), 4);
        let v: Vec<_> = b.iter_ones().collect();
        assert_eq!(v, vec![0, 63, 64, 129]);
        b.clear(64);
        assert_eq!(b.count_ones(), 3);
        assert!(b.test(129));
    }

    #[test]
    fn ones_honors_length_no_padding_bits() {
        let b = BitSet::ones(65);
        assert_eq!(b.count_ones(), 65);
    }
}
