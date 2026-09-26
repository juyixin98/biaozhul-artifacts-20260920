//! Code-length table construction (deterministic Huffman) and canonical
//! code assignment / validation. The core algorithm is implemented here
//! from scratch; no external compression crates are used.

use crate::error::{Error, Result};
use std::cmp::Reverse;
use std::collections::BinaryHeap;

/// Maximum allowed code length in bits. Encoder output is length-limited to
/// this value; decoder rejects tables containing longer codes.
pub const MAX_CODE_LEN: u8 = 32;

/// Build code lengths from symbol frequencies.
///
/// Deterministic: heap entries are ordered by `(frequency, min_symbol)`,
/// which is a total order, so equal-frequency inputs always yield the same
/// tree. If the natural Huffman tree exceeds `MAX_CODE_LEN`, frequencies are
/// halved (keeping every present symbol at >= 1) and the tree is rebuilt;
/// this length-limiting heuristic always terminates because collapsed
/// frequencies converge to a balanced tree.
pub fn build_lengths(freqs: &[u64; 256]) -> [u8; 256] {
    let mut shift = 0u32;
    loop {
        let scaled = if shift == 0 { *freqs } else { scale(freqs, shift) };
        let lens = huffman_lengths(&scaled);
        let max = lens.iter().copied().max().unwrap_or(0);
        if max <= MAX_CODE_LEN {
            return lens;
        }
        shift += 1;
        debug_assert!(shift < 64, "length limiting failed to converge");
    }
}

fn scale(freqs: &[u64; 256], shift: u32) -> [u64; 256] {
    let mut out = [0u64; 256];
    for (i, &f) in freqs.iter().enumerate() {
        if f > 0 {
            out[i] = (f >> shift).max(1);
        }
    }
    out
}

/// Raw Huffman tree construction. Returns per-symbol code lengths (0 = absent).
fn huffman_lengths(freqs: &[u64; 256]) -> [u8; 256] {
    let mut lengths = [0u8; 256];
    let present: Vec<usize> = (0..256).filter(|&s| freqs[s] > 0).collect();
    match present.len() {
        0 => return lengths,
        // A single distinct symbol gets a 1-bit code so the stream is
        // self-delimiting without a special case.
        1 => {
            lengths[present[0]] = 1;
            return lengths;
        }
        _ => {}
    }

    // Nodes 0..256 are leaves; internal nodes are appended. `parent` maps a
    // node to its parent; the root keeps `usize::MAX`.
    let mut parent: Vec<usize> = vec![usize::MAX; 256];
    // (frequency, min_symbol, node_id): min_symbol is unique across live
    // nodes because their symbol sets are disjoint, giving a total order.
    let mut heap: BinaryHeap<Reverse<(u64, u32, usize)>> = BinaryHeap::new();
    for &s in &present {
        heap.push(Reverse((freqs[s], s as u32, s)));
    }
    while heap.len() > 1 {
        let Reverse((f1, m1, n1)) = heap.pop().unwrap();
        let Reverse((f2, m2, n2)) = heap.pop().unwrap();
        let id = parent.len();
        parent.push(usize::MAX);
        parent[n1] = id;
        parent[n2] = id;
        heap.push(Reverse((f1 + f2, m1.min(m2), id)));
    }
    for &s in &present {
        let mut depth = 0u32;
        let mut n = s;
        while parent[n] != usize::MAX {
            n = parent[n];
            depth += 1;
        }
        lengths[s] = depth as u8; // 256 leaves => depth <= 255, fits u8
    }
    lengths
}

/// A validated canonical code table shared by encoder and decoder.
///
/// Canonical assignment: symbols are sorted by `(length, symbol)`; codes are
/// then assigned consecutively per length class, MSB-first.
#[derive(Clone, Debug)]
pub struct CodeTable {
    /// Code length per symbol (0 = symbol absent).
    pub lengths: [u8; 256],
    codes: [u64; 256],
    counts: [u32; 33],
    first_code: [u64; 33],
    first_index: [u32; 33],
    sorted: Vec<u8>,
    max_len: u8,
    /// True when the Kraft sum equals exactly 1.
    pub complete: bool,
}

impl CodeTable {
    /// Validate a length table and derive canonical codes.
    ///
    /// Rejects lengths > `MAX_CODE_LEN` and oversubscribed tables
    /// (Kraft sum > 1). Incomplete tables (Kraft sum < 1) are accepted here;
    /// whether to use them is a decode-time policy decision.
    pub fn from_lengths(lengths: [u8; 256]) -> Result<CodeTable> {
        for &l in lengths.iter() {
            if l > MAX_CODE_LEN {
                return Err(Error::InvalidCodeLength(l));
            }
        }
        // Kraft sum in units of 2^-32 so it fits a u64 exactly.
        let mut kraft: u64 = 0;
        for &l in lengths.iter() {
            if l > 0 {
                kraft += 1u64 << (MAX_CODE_LEN - l);
            }
        }
        let full = 1u64 << MAX_CODE_LEN;
        if kraft > full {
            return Err(Error::Oversubscribed);
        }
        let complete = kraft == full;

        let mut counts = [0u32; 33];
        let mut max_len = 0u8;
        for &l in lengths.iter() {
            if l > 0 {
                counts[l as usize] += 1;
                max_len = max_len.max(l);
            }
        }

        let mut sorted: Vec<u8> = (0..256u32)
            .filter(|&s| lengths[s as usize] > 0)
            .map(|s| s as u8)
            .collect();
        sorted.sort_by_key(|&s| (lengths[s as usize], s));

        let mut first_code = [0u64; 33];
        let mut first_index = [0u32; 33];
        let mut code = 0u64;
        let mut index = 0u32;
        for l in 1..=MAX_CODE_LEN as usize {
            first_code[l] = code;
            first_index[l] = index;
            code = (code + counts[l] as u64) << 1;
            index += counts[l];
        }

        let mut codes = [0u64; 256];
        let mut next = first_code;
        for &s in &sorted {
            let l = lengths[s as usize] as usize;
            codes[s as usize] = next[l];
            next[l] += 1;
        }

        Ok(CodeTable {
            lengths,
            codes,
            counts,
            first_code,
            first_index,
            sorted,
            max_len,
            complete,
        })
    }

    /// Canonical code and length for a symbol. Length is 0 when absent.
    pub fn code(&self, sym: u8) -> (u64, u8) {
        (self.codes[sym as usize], self.lengths[sym as usize])
    }

    /// Number of symbols with a non-zero code length.
    pub fn symbol_count(&self) -> usize {
        self.sorted.len()
    }

    /// Decode one symbol by pulling bits until a code matches.
    ///
    /// `read_bit` returns `Ok(None)` at end of stream, which maps to
    /// `Error::Truncated`. With an incomplete table, a bit sequence that
    /// matches no code yields `Error::UndefinedCode`.
    pub fn decode_symbol<F>(&self, mut read_bit: F) -> Result<u8>
    where
        F: FnMut() -> Result<Option<u64>>,
    {
        let mut code = 0u64;
        for l in 1..=self.max_len as usize {
            match read_bit()? {
                None => return Err(Error::Truncated),
                Some(bit) => {
                    code = (code << 1) | bit;
                    let count = self.counts[l] as u64;
                    if count > 0 {
                        let first = self.first_code[l];
                        if code >= first && code - first < count {
                            let idx = self.first_index[l] as u64 + (code - first);
                            return Ok(self.sorted[idx as usize]);
                        }
                    }
                }
            }
        }
        Err(Error::UndefinedCode)
    }
}
