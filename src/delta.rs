//! Rolling delta engine.
//!
//! Given an old artifact's [`Signature`] and the new `target` bytes, produce a
//! stream of operations:
//!
//! - `Ref { index, length }` — this target slice is byte-identical to old
//!   block `index` (confirmed by strong digest);
//! - `Literal(bytes)` — fresh bytes the client must send.
//!
//! The weak rolling checksum only *locates* candidates.  A candidate is turned
//! into a `Ref` strictly when its strong digest equals the digest of the
//! actual target bytes (`strong_actual == strong_candidate`).  Repeated old
//! blocks and inserted weak-checksum collisions are therefore both handled
//! correctly: the weak key maps to a *list* of candidates, and content
//! equality is never decided on the weak value.

use crate::signature::{strong_hash, BlockSig, SigIndex, Signature};
use crate::weak::Rollsum;

/// One patch operation against the old artifact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    /// Raw new bytes not present in the old artifact.
    Literal(Vec<u8>),
    /// Copy block `index` (`length` bytes) from the old artifact.
    Ref { index: u32, length: u32 },
}

/// Diagnostics describing how much content was reused vs. transmitted.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeltaStats {
    pub target_len: u64,
    pub basis_len: u64,
    /// Weak-key hits that became strong-confirmed refs.
    pub blocks_matched: u64,
    pub bytes_from_basis: u64,
    /// New bytes emitted as literals.
    pub literal_bytes: u64,
    /// Number of weak-key lookups that returned at least one candidate.
    pub weak_hits: u64,
    /// Candidates whose weak key matched but strong digest differed.
    pub strong_rejections: u64,
}

/// Result of a delta computation.
#[derive(Debug, Clone)]
pub struct Delta {
    pub ops: Vec<Op>,
    pub stats: DeltaStats,
    pub target_len: u64,
    pub target_hash: [u8; 32],
}

/// Among blocks sharing the target window's weak checksum, find one that is
/// the same length AND has the same strong digest as the actual window bytes.
///
/// The window is hashed at most once, and only because the weak key matched
/// at least one block.  Candidates of the wrong length are skipped without
/// being counted as digest rejections.
fn strong_match<'a>(
    index: &'a SigIndex,
    candidates: &[u32],
    actual: &[u8],
    stats: &mut DeltaStats,
) -> Option<&'a BlockSig> {
    let mut hash: Option<[u8; 32]> = None;
    for &cand in candidates {
        let block = &index.blocks[cand as usize];
        if block.length as usize != actual.len() {
            continue;
        }
        let actual_hash = *hash.get_or_insert_with(|| strong_hash(actual));
        if block.strong == actual_hash {
            return Some(block);
        }
        stats.strong_rejections += 1;
    }
    None
}

/// Compute the delta of `target` against a basis described by `sig`.
#[allow(unused_assignments)] // macro-tracked literal cursor; final writes terminate the run
pub fn diff(sig: &Signature, basis_len: u64, target: &[u8]) -> Delta {
    let index = SigIndex::new(sig.clone());
    let s = index.block_len;
    let n = target.len();

    let mut stats = DeltaStats {
        target_len: n as u64,
        basis_len,
        ..Default::default()
    };
    let mut ops: Vec<Op> = Vec::new();

    // Start of the literal run pending emission.
    #[allow(unused_assignments)]
    let mut lit_start: usize = 0;
    macro_rules! flush_literal {
        ($end:expr) => {{
            let end = $end;
            if end > lit_start {
                stats.literal_bytes += (end - lit_start) as u64;
                ops.push(Op::Literal(target[lit_start..end].to_vec()));
            }
            lit_start = end;
        }};
    }

    let mut pos = 0usize;
    // First byte not yet consumed by a matched full block. After the loop it
    // equals `n` when the final full window matched (its s bytes include any
    // trailing residue), otherwise it is at most `n - s`.
    if n >= s {
        // Prime the first window [win_start, win_start + s).
        let mut rs = Rollsum::new();
        for &b in &target[..s] {
            rs.feed(b);
        }
        let mut win_start = 0usize;
        let last_full_start = n - s;
        loop {
            let candidates = index.weak_hits(rs.value());
            let mut matched: Option<&BlockSig> = None;
            if !candidates.is_empty() {
                stats.weak_hits += 1;
                matched = strong_match(
                    &index,
                    candidates,
                    &target[win_start..win_start + s],
                    &mut stats,
                );
            }
            if let Some(block) = matched {
                // Emit the literal bytes scanned between the previous match
                // and this window (insertions/deletions land here).
                if lit_start < win_start {
                    flush_literal!(win_start);
                }
                ops.push(Op::Ref { index: block.index, length: block.length });
                stats.blocks_matched += 1;
                stats.bytes_from_basis += block.length as u64;
                // Re-prime right after the match and keep rolling one byte at
                // a time; we never jump, so shifted blocks stay discoverable.
                let next = win_start + s;
                lit_start = next;
                pos = next;
                if next > last_full_start {
                    break;
                }
                rs = Rollsum::new();
                for &b in &target[next..next + s] {
                    rs.feed(b);
                }
                win_start = next;
                continue;
            }
            if win_start == last_full_start {
                break;
            }
            rs.roll(target[win_start], target[win_start + s], s);
            win_start += 1;
        }
    }

    // Trailing residue: when n is not a multiple of s and the final full
    // window did not already consume the tail, the final `n % s` bytes may
    // still equal the basis's trailing short block. The `pos <= short_start`
    // guard forbids an overlapping ref when a matched full block already
    // extends into (or past) the residue.
    let short_len = n % s;
    if short_len > 0 && pos < n && pos <= n - short_len {
        let short_start = n - short_len;
        let tail = &target[short_start..];
        let candidates = index.weak_hits(Rollsum::checksum(tail));
        if !candidates.is_empty() {
            stats.weak_hits += 1;
            if let Some(block) = strong_match(&index, candidates, tail, &mut stats) {
                if lit_start < short_start {
                    flush_literal!(short_start);
                }
                ops.push(Op::Ref { index: block.index, length: block.length });
                stats.blocks_matched += 1;
                stats.bytes_from_basis += block.length as u64;
                lit_start = n;
            }
        }
    }

    // Everything not emitted as a ref is literal.
    flush_literal!(n);

    Delta {
        ops,
        stats,
        target_len: n as u64,
        target_hash: strong_hash(target),
    }
}
