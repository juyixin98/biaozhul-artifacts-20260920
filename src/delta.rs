//! Block-level delta computation and patch application.
//!
//! ## Algorithm (rsync-style)
//!
//! 1. The basis is split into fixed-size blocks. For each block the signature
//!    contains a 32-bit weak rolling checksum and a BLAKE3 strong hash.
//! 2. A hash map from weak checksum → candidate basis blocks lets us locate
//!    possible matches in O(1) while rolling a window over the target.
//! 3. The window advances one byte at a time using the O(1) roll formula. On a
//!    weak hit we *still* compute BLAKE3 over the window and compare it with
//!    the candidate's strong hash. Only a strong match emits a COPY.
//! 4. Unmatched bytes are emitted as LITERALs. After the full-size scan, a
//!    second short-window scan runs over the leftover (< block size) tail so
//!    that basis tail blocks can be matched at arbitrary insert/delete offsets.
//!
//! Duplicate basis blocks are all stored as candidates for the same weak key;
//! the first strong-confirmed candidate is used. The weak checksum therefore
//! never decides equality — a deliberately constructed weak collision whose
//! BLAKE3 differs is simply treated as "no match".

use std::collections::HashMap;

use base64::Engine;

use crate::checksum::{self, pack_parts, roll, weak_parts};
use crate::protocol::{Delta, Op, Signature};
use crate::Error;

/// A basis block that may match a rolling window.
#[derive(Clone, Debug)]
struct Candidate {
    /// Block index in the basis.
    index: usize,
    /// Block length in bytes (only the last block may be shorter).
    len: usize,
    /// Strong BLAKE3 digest; the sole authority for content equality.
    strong: [u8; 32],
}

/// Weak-checksum → candidate blocks index built from a [`Signature`].
pub struct WeakIndex {
    map: HashMap<u32, Vec<Candidate>>,
}

impl WeakIndex {
    pub fn from_signature(sig: &Signature) -> Result<Self, Error> {
        let mut map: HashMap<u32, Vec<Candidate>> = HashMap::with_capacity(sig.blocks.len());
        for (i, block) in sig.blocks.iter().enumerate() {
            let raw = crate::hex::decode(&block.strong_hex)
                .map_err(|e| Error::bad_request(format!("invalid strong hash in block {i}: {e}")))?;
            if raw.len() != 32 {
                return Err(Error::bad_request(format!(
                    "strong hash in block {i} is not 32 bytes"
                )));
            }
            let mut strong = [0u8; 32];
            strong.copy_from_slice(&raw);
            let start = i * sig.block_size;
            let len = sig
                .basis_len
                .saturating_sub(start)
                .min(sig.block_size);
            if len == 0 {
                return Err(Error::bad_request(format!(
                    "signature block {i} lies outside basis_len"
                )));
            }
            map.entry(block.weak).or_default().push(Candidate {
                index: i,
                len,
                strong,
            });
        }
        Ok(WeakIndex { map })
    }

    /// Locate candidates by weak checksum, then confirm with the strong hash
    /// over the actual window. Returns the matching block index, if any.
    ///
    /// This is the key correctness boundary: a weak collision with a different
    /// strong hash returns `None`.
    fn confirm(&self, weak: u32, window: &[u8]) -> Option<usize> {
        let cands = self.map.get(&weak)?;
        let window_hash = checksum::strong(window);
        cands
            .iter()
            .find(|c| c.len == window.len() && c.strong == window_hash)
            .map(|c| c.index)
    }
}

/// Collects delta ops while buffering one contiguous run of literal bytes.
struct Emitter {
    ops: Vec<Op>,
    pending: Vec<u8>,
}

impl Emitter {
    fn new() -> Self {
        Emitter {
            ops: Vec::new(),
            pending: Vec::new(),
        }
    }

    fn push_byte(&mut self, b: u8) {
        self.pending.push(b);
    }

    fn push_slice(&mut self, s: &[u8]) {
        self.pending.extend_from_slice(s);
    }

    /// Emit a COPY, first flushing any buffered literal bytes.
    fn copy(&mut self, block_index: usize) {
        self.flush_literal();
        self.ops.push(Op::Copy { block_index });
    }

    fn flush_literal(&mut self) {
        if !self.pending.is_empty() {
            let data = std::mem::take(&mut self.pending);
            let data_b64 = base64::engine::general_purpose::STANDARD.encode(data);
            self.ops.push(Op::Literal { data_b64 });
        }
    }

    fn finish(mut self) -> Vec<Op> {
        self.flush_literal();
        self.ops
    }
}

/// Compute the delta from `basis` (described by `sig`) to `target`.
pub fn compute_delta(target: &[u8], sig: &Signature) -> Result<Delta, Error> {
    if !(checksum::MIN_BLOCK_SIZE..=checksum::MAX_BLOCK_SIZE).contains(&sig.block_size) {
        return Err(Error::bad_request(format!(
            "block_size must be in {}..={}",
            checksum::MIN_BLOCK_SIZE,
            checksum::MAX_BLOCK_SIZE
        )));
    }
    let index = WeakIndex::from_signature(sig)?;
    let n = sig.block_size;
    let mut out = Emitter::new();
    let len = target.len();

    // ---- Phase A: roll full-size windows over the target ------------------
    let mut pos = 0usize;
    if pos + n <= len {
        let mut parts = weak_parts(&target[pos..pos + n]);
        while pos + n <= len {
            match index.confirm(pack_parts(parts), &target[pos..pos + n]) {
                Some(block_index) => {
                    // Strong-confirmed match: emit COPY and jump the window.
                    out.copy(block_index);
                    pos += n;
                    if pos + n <= len {
                        parts = weak_parts(&target[pos..pos + n]);
                    }
                }
                None => {
                    // Weak miss (or a weak collision the strong hash rejected):
                    // this byte is literal; roll the window forward by one.
                    let removed = target[pos];
                    pos += 1;
                    out.push_byte(removed);
                    if pos + n <= len {
                        let added = target[pos + n - 1];
                        parts = roll(parts, removed, added, n);
                    }
                }
            }
        }
    }
    // Bytes after the last full-size window (fewer than n) are not consumed
    // by the rolling loop; queue them for the short-tail phase.
    out.push_slice(&target[pos..]);

    // ---- Phase B: short tail block matching -------------------------------
    // The leftover `target[pos..]` is shorter than n. If the basis's last
    // block is itself short, roll a window of that exact length over the
    // leftover so a tail copy can still be found after inserts/deletes.
    let tail = std::mem::take(&mut out.pending);
    let tail_len = tail.len();
    let num_blocks = sig.blocks.len();
    let last_short_len = sig.basis_len % n; // 0 when the basis length is a multiple of n
    if tail_len >= last_short_len
        && num_blocks > 0
        && last_short_len > 0
        && last_short_len < n
    {
        let m = last_short_len;
        let mut tpos = 0usize;
        let mut scan_start = 0usize;
        let mut parts = weak_parts(&tail[tpos..tpos + m]);
        while tpos + m <= tail_len {
            match index.confirm(pack_parts(parts), &tail[tpos..tpos + m]) {
                Some(block_index) => {
                    out.push_slice(&tail[scan_start..tpos]);
                    out.copy(block_index);
                    tpos += m;
                    scan_start = tpos;
                    if tpos + m <= tail_len {
                        parts = weak_parts(&tail[tpos..tpos + m]);
                    }
                }
                None => {
                    let removed = tail[tpos];
                    tpos += 1;
                    if tpos + m <= tail_len {
                        let added = tail[tpos + m - 1];
                        parts = roll(parts, removed, added, m);
                    }
                }
            }
        }
        out.push_slice(&tail[scan_start..]);
    } else {
        // No short tail block possible (empty basis, aligned basis, or the
        // leftover is smaller than the tail block): every leftover byte is
        // literal.
        out.push_slice(&tail);
    }

    Ok(Delta {
        block_size: n,
        basis_len: sig.basis_len,
        ops: out.finish(),
    })
}

/// Apply `delta` to `basis`, reconstructing the target artifact.
pub fn apply_delta(basis: &[u8], delta: &Delta) -> Result<Vec<u8>, Error> {
    if basis.len() != delta.basis_len {
        return Err(Error::bad_request(format!(
            "basis length {} does not match delta.basis_len {}",
            basis.len(),
            delta.basis_len
        )));
    }
    let n = delta.block_size;
    if !(checksum::MIN_BLOCK_SIZE..=checksum::MAX_BLOCK_SIZE).contains(&n) {
        return Err(Error::bad_request("invalid block_size in delta"));
    }

    // Pre-size: copied bytes plus literal bytes.
    let est: usize = delta
        .ops
        .iter()
        .map(|op| match op {
            Op::Copy { .. } => n,
            Op::Literal { data_b64 } => data_b64.len() * 3 / 4,
        })
        .sum();
    let mut result = Vec::with_capacity(est);

    for op in &delta.ops {
        match op {
            Op::Literal { data_b64 } => {
                let data = base64::engine::general_purpose::STANDARD
                    .decode(data_b64.as_bytes())
                    .map_err(|e| Error::bad_request(format!("invalid base64 literal: {e}")))?;
                result.extend_from_slice(&data);
            }
            Op::Copy { block_index } => {
                let start = block_index
                    .checked_mul(n)
                    .ok_or_else(|| Error::bad_request("block_index overflow"))?;
                if start >= basis.len() {
                    return Err(Error::bad_request(format!(
                        "copy block_index {block_index} out of range"
                    )));
                }
                let end = (start + n).min(basis.len());
                result.extend_from_slice(&basis[start..end]);
            }
        }
    }
    Ok(result)
}
