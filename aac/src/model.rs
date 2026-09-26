//! Adaptive order-0 frequency model.
//!
//! The alphabet is the 256 byte values plus one EOF terminator symbol
//! ([`EOF_SYMBOL`](crate::constants::EOF_SYMBOL)). Every symbol starts with
//! count [`INITIAL_FREQUENCY`]. After a symbol is encoded/decoded its count is
//! incremented by one; when the total count exceeds
//! [`MAX_FREQUENCY`] every count is halved (floor) with a floor of
//! [`RESCALE_FLOOR`]. These update and rescaling rules are fixed and identical
//! for encoder and decoder, so the two sides stay synchronized symbol by
//! symbol.
//!
//! Prefix sums and cumulative-frequency lookup use a Fenwick (binary indexed)
//! tree, giving O(log N) updates and searches over 257 symbols.

use crate::constants::{INITIAL_FREQUENCY, MAX_FREQUENCY, NUM_SYMBOLS, RESCALE_FLOOR};

/// Adaptive frequency table for the 257-symbol alphabet.
#[derive(Clone)]
pub struct Model {
    /// Fenwick tree (1-based indexing): cumulative-frequency structure.
    tree: [u32; NUM_SYMBOLS + 1],
    /// Raw per-symbol counts (0-based symbol index).
    counts: [u32; NUM_SYMBOLS],
    /// Sum of all counts.
    total: u32,
    /// Number of times [`rescale`](Self::rescale) has run.
    rescales: u64,
}

impl Default for Model {
    fn default() -> Self {
        Self::new()
    }
}

impl Model {
    /// Build a fresh model with [`INITIAL_FREQUENCY`] for every symbol.
    pub fn new() -> Self {
        let mut model = Model {
            tree: [0u32; NUM_SYMBOLS + 1],
            counts: [INITIAL_FREQUENCY; NUM_SYMBOLS],
            total: INITIAL_FREQUENCY * NUM_SYMBOLS as u32,
            rescales: 0,
        };
        model.rebuild_tree();
        model
    }

    /// Total frequency (denominator of every probability interval).
    #[inline]
    pub fn total(&self) -> u32 {
        self.total
    }

    /// How many rescales have occurred on this model instance.
    #[inline]
    pub fn rescale_count(&self) -> u64 {
        self.rescales
    }

    /// Raw count of one symbol (mainly useful for tests and diagnostics).
    #[inline]
    pub fn count(&self, symbol: u16) -> u32 {
        self.counts[symbol as usize]
    }

    /// Rebuild the Fenwick tree from [`Self::counts`] in O(N).
    fn rebuild_tree(&mut self) {
        self.tree = [0u32; NUM_SYMBOLS + 1];
        for i in 1..=NUM_SYMBOLS {
            self.tree[i] += self.counts[i - 1];
            let parent = i + (i & i.wrapping_neg());
            if parent <= NUM_SYMBOLS {
                self.tree[parent] += self.tree[i];
            }
        }
    }

    /// Add one to one symbol's count in the tree.
    #[inline]
    fn tree_add_one(&mut self, symbol: u16) {
        let mut i = symbol as usize + 1;
        while i <= NUM_SYMBOLS {
            self.tree[i] += 1;
            i += i & i.wrapping_neg();
        }
    }

    /// Cumulative frequency strictly below `symbol`: the sum of counts of
    /// symbols `0 .. symbol`.
    #[inline]
    pub fn cumulative_low(&self, symbol: u16) -> u32 {
        // Symbol s occupies 1-based Fenwick position s+1, so symbols 0..s-1
        // occupy positions 1..=s.
        let mut i = symbol as usize;
        let mut sum = 0u32;
        while i > 0 {
            sum += self.tree[i];
            i -= i & i.wrapping_neg();
        }
        sum
    }

    /// Find the symbol whose cumulative interval contains target value `v`
    /// (`0 <= v < total`).
    ///
    /// Returns `(symbol, cumulative_low, frequency)`. This is the inverse of
    /// [`cumulative_low`](Self::cumulative_low): for `v` in a symbol's
    /// interval the same `(cum, freq)` is returned that the encoder used.
    pub fn find(&self, v: u32) -> (u16, u32, u32) {
        let mut idx = 0usize; // 1-based Fenwick index reached
        let mut sum = 0u32; // prefix sum up to and including `idx`
                            // Largest power of two not exceeding the tree size (258 -> 256).
        let n_pow2 = (NUM_SYMBOLS + 1).next_power_of_two() >> 1;
        let mut bit_mask: usize = n_pow2;
        while bit_mask != 0 {
            let next = idx + bit_mask;
            if next <= NUM_SYMBOLS && sum + self.tree[next] <= v {
                idx = next;
                sum += self.tree[next];
            }
            bit_mask >>= 1;
        }
        // `idx` is the largest 1-based index with prefix(idx) <= v.
        // Symbol is therefore 0-based symbol `idx`.
        let symbol = idx as u16;
        let cum = sum;
        let freq = self.counts[symbol as usize];
        (symbol, cum, freq)
    }

    /// Observe a symbol: increment its count, rescaling first if the model is
    /// full. Returns the number of rescales triggered by this update (0 or 1).
    ///
    /// Encoder and decoder must call this in exactly the same order, i.e. once
    /// per symbol after the symbol has been coded.
    pub fn update(&mut self, symbol: u16) -> u64 {
        let mut triggered = 0;
        if self.total >= MAX_FREQUENCY {
            self.rescale();
            triggered = 1;
        }
        self.counts[symbol as usize] += 1;
        self.tree_add_one(symbol);
        self.total += 1;
        triggered
    }

    /// Fixed rescaling rule: every count becomes
    /// `max(RESCALE_FLOOR, floor(count / 2))`.
    ///
    /// Halving (rather than rebuilding) preserves the learned relative
    /// distribution while making room for new evidence; the floor guarantees a
    /// symbol never gets probability zero once present in the alphabet (all
    /// symbols start present, so the floor applies to all 257 of them).
    fn rescale(&mut self) {
        let mut new_total = 0u32;
        for c in self.counts.iter_mut() {
            let halved = *c / 2;
            *c = halved.max(RESCALE_FLOOR);
            new_total += *c;
        }
        self.total = new_total;
        self.rebuild_tree();
        self.rescales += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::constants::EOF_SYMBOL;

    #[test]
    fn initial_uniform_distribution() {
        let m = Model::new();
        assert_eq!(m.total() as usize, NUM_SYMBOLS);
        for s in 0..NUM_SYMBOLS as u16 {
            assert_eq!(m.count(s), INITIAL_FREQUENCY);
        }
        assert_eq!(m.cumulative_low(EOF_SYMBOL), EOF_SYMBOL as u32);
    }

    #[test]
    fn find_is_inverse_of_cumulative_intervals() {
        let mut m = Model::new();
        // Bias the model irregularly.
        for s in [0u16, 0, 5, 5, 5, 200, EOF_SYMBOL] {
            m.update(s);
        }
        for s in 0..NUM_SYMBOLS as u16 {
            let low = m.cumulative_low(s);
            let high = low + m.count(s);
            for v in low..high {
                let (found, cum, freq) = m.find(v);
                assert_eq!(found, s, "value {v} resolved to wrong symbol");
                assert_eq!(cum, low);
                assert_eq!(freq, m.count(s));
            }
        }
    }

    #[test]
    fn rescale_keeps_total_bounded_and_floors_counts() {
        let mut m = Model::new();
        let mut rescales = 0u64;
        for i in 0..100_000u32 {
            let sym = (i % 4) as u16; // heavily skewed stream
            rescales += m.update(sym);
        }
        assert!(
            rescales > 5,
            "skewed stream should rescale repeatedly: {rescales}"
        );
        assert!(
            m.total() < MAX_FREQUENCY + 2,
            "total overflows cap: {}",
            m.total()
        );
        assert!(m.count(0) >= RESCALE_FLOOR);
        assert!(m.count(EOF_SYMBOL) >= RESCALE_FLOOR);
        // find must still partition the whole range
        let (sym, cum, freq) = m.find(m.total() - 1);
        assert_eq!(cum + freq, m.total());
        assert_eq!(sym, EOF_SYMBOL); // EOF is the last symbol and has the largest v
    }

    #[test]
    fn encoder_decoder_model_mirror_each_other() {
        let mut enc = Model::new();
        let mut dec = Model::new();
        for (i, s) in [1u16, 2, 2, 255, EOF_SYMBOL, 0, 0].iter().enumerate() {
            let low = enc.cumulative_low(*s);
            let freq = enc.count(*s);
            let total = enc.total();
            // decoder performs the inverse lookup then the same update
            let (found, dl, df) = dec.find(low);
            assert_eq!(found, *s, "step {i}");
            assert_eq!((dl, df), (low, freq));
            assert_eq!(dec.total(), total);
            enc.update(*s);
            dec.update(found);
        }
    }
}
