//! Minimal fixed-size bitmap used as the IEJoin candidate set.
//!
//! The bitmap has one slot per *active* build-side row, addressed by the
//! row's rank in the second predicate's sort order. Set bits mark rows that
//! have already been admitted by the first (merge) predicate and therefore
//! remain candidates for the second predicate.

/// 64-bit word-addressed bitmap.
#[derive(Debug, Clone, Default)]
pub struct Bitmap {
    words: Vec<u64>,
    len: usize,
}

impl Bitmap {
    pub fn new(len: usize) -> Self {
        let words = len.div_ceil(64);
        Self {
            words: vec![0; words],
            len,
        }
    }

    #[inline]
    pub fn set(&mut self, rank: usize) {
        debug_assert!(rank < self.len);
        self.words[rank >> 6] |= 1u64 << (rank & 63);
    }

    #[inline]
    pub fn get(&self, rank: usize) -> bool {
        debug_assert!(rank < self.len);
        (self.words[rank >> 6] >> (rank & 63)) & 1 == 1
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// Number of 64-bit words overlapping the half-open rank window
    /// `[lo, hi)` — the physical word probes needed to inspect it.
    #[inline]
    pub fn word_probes(lo: usize, hi: usize) -> u64 {
        if hi <= lo {
            return 0;
        }
        let first = lo >> 6;
        let last = (hi - 1) >> 6;
        (last - first + 1) as u64
    }

    /// Iterate every set rank inside `[lo, hi)`.
    ///
    /// The window is scanned word by word (zero words cost one probe and no
    /// bit work); each non-zero word yields its set ranks low-bit-first.
    #[inline]
    pub fn for_each_set_in(&self, lo: usize, hi: usize, mut emit: impl FnMut(usize)) {
        self.for_each_set_in_while(lo, hi, |rank| {
            emit(rank);
            true
        });
    }

    /// Like [`Bitmap::for_each_set_in`] but stops as soon as `emit` returns
    /// `false` (used for exact output-budget truncation).
    #[inline]
    pub fn for_each_set_in_while(&self, lo: usize, hi: usize, mut emit: impl FnMut(usize) -> bool) {
        if hi <= lo || self.len == 0 {
            return;
        }
        let lo = lo.min(self.len);
        let hi = hi.min(self.len);
        if hi <= lo {
            return;
        }
        let first_word = lo >> 6;
        let last_word = (hi - 1) >> 6;
        let lo_bit = (lo & 63) as u32;
        let hi_rem = (hi & 63) as u32;
        for word_idx in first_word..=last_word {
            let mut word = self.words[word_idx];
            if word_idx == first_word && lo_bit != 0 {
                word &= u64::MAX << lo_bit;
            }
            if word_idx == last_word && hi_rem != 0 {
                word &= (1u64 << hi_rem) - 1;
            }
            while word != 0 {
                let tz = word.trailing_zeros() as usize;
                if !emit((word_idx << 6) + tz) {
                    return;
                }
                word &= word - 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_get_and_window_probes() {
        let mut bm = Bitmap::new(130);
        assert!(!bm.get(0));
        bm.set(0);
        bm.set(63);
        bm.set(64);
        bm.set(129);
        assert!(bm.get(0));
        assert!(bm.get(63));
        assert!(bm.get(64));
        assert!(bm.get(129));
        assert_eq!(Bitmap::word_probes(0, 64), 1);
        assert_eq!(Bitmap::word_probes(1, 64), 1);
        assert_eq!(Bitmap::word_probes(63, 65), 2);
        assert_eq!(Bitmap::word_probes(0, 130), 3);
        assert_eq!(Bitmap::word_probes(10, 10), 0);
    }

    #[test]
    fn iterates_ranks_in_window_only() {
        let mut bm = Bitmap::new(200);
        for r in [0, 5, 63, 64, 100, 127, 128, 199] {
            bm.set(r);
        }
        let got = |lo, hi| {
            let mut out = Vec::new();
            bm.for_each_set_in(lo, hi, |r| out.push(r));
            out
        };
        assert_eq!(got(0, 64), vec![0, 5, 63]);
        assert_eq!(got(1, 64), vec![5, 63]);
        assert_eq!(got(63, 129), vec![63, 64, 100, 127, 128]);
        assert_eq!(got(64, 128), vec![64, 100, 127]);
        assert_eq!(got(130, 200), vec![199]);
        assert!(got(200, 250).is_empty());
    }
}
