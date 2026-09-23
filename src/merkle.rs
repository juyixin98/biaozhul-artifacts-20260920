//! Merkle tree over fixed-size blocks of a file.
//!
//! # Canonical hashing rules
//!
//! - Leaf `i` committing to block bytes `b`:
//!   `SHA256( 0x00 || i_be_u64 || b.len()_be_u64 || b )`
//!   Index and length are inside the hash, so blocks cannot be reordered and
//!   the final (short) block cannot be padded to forge a different file length.
//! - Internal node with children L, R:
//!   `SHA256( 0x01 || L || R )`
//! - **Odd-node rule** (Certificate-Transparency style): when a level has an
//!   odd number of nodes, the last node is promoted to the next level
//!   unchanged — it is NOT duplicated and NOT hashed with itself.
//! - Empty file (zero leaves): root is the fixed sentinel
//!   `SHA256("merkle-store:empty-root-v1")`, distinct by construction from any
//!   leaf/internal hash.
//!
//! # Range proofs
//!
//! A proof for blocks `[start, end)` contains:
//! 1. the leaf hashes for every requested block (recomputed by the verifier
//!    from the supplied block bytes), and
//! 2. sibling/sub-tree hashes needed to rebuild the root, each tagged with its
//!    `(level, index)` so reordered or misplaced nodes ("错位证明") are rejected
//!    structurally, before any hash comparison.
//!
//! The verifier only touches the bytes of the requested blocks plus the O(log n)
//! proof hashes; it never reads the complete file.

use crate::sha256::sha256;

pub type Hash32 = [u8; 32];

const LEAF_DOMAIN: u8 = 0x00;
const INTERNAL_DOMAIN: u8 = 0x01;
const EMPTY_ROOT_TAG: &[u8] = b"merkle-store:empty-root-v1";

/// Root of the zero-leaf tree.
pub fn empty_root() -> Hash32 {
    sha256(EMPTY_ROOT_TAG)
}

/// Commitment of block `index` to its exact bytes.
pub fn leaf_hash(index: usize, block: &[u8]) -> Hash32 {
    let mut input = Vec::with_capacity(1 + 8 + 8 + block.len());
    input.push(LEAF_DOMAIN);
    input.extend_from_slice(&(index as u64).to_be_bytes());
    input.extend_from_slice(&(block.len() as u64).to_be_bytes());
    input.extend_from_slice(block);
    sha256(&input)
}

/// Commitment of an ordered pair of child nodes.
pub fn internal_hash(left: &Hash32, right: &Hash32) -> Hash32 {
    let mut input = Vec::with_capacity(1 + 64);
    input.push(INTERNAL_DOMAIN);
    input.extend_from_slice(left);
    input.extend_from_slice(right);
    sha256(&input)
}

/// Number of nodes `level` holds for a tree with `n` leaves.
pub fn level_count(n: usize, level: usize) -> usize {
    let mut c = n;
    for _ in 0..level {
        c = c.div_ceil(2);
    }
    c
}

/// Build all levels from leaf hashes. `result[0]` is the leaves themselves.
pub fn build_levels(leaves: &[Hash32]) -> Vec<Vec<Hash32>> {
    let mut levels: Vec<Vec<Hash32>> = Vec::new();
    levels.push(leaves.to_vec());
    while levels.last().unwrap().len() > 1 {
        let cur = levels.last().unwrap();
        let mut next = Vec::with_capacity(cur.len().div_ceil(2));
        let mut i = 0;
        while i + 1 < cur.len() {
            next.push(internal_hash(&cur[i], &cur[i + 1]));
            i += 2;
        }
        if i < cur.len() {
            // Odd trailing node: promote unchanged.
            next.push(cur[i]);
        }
        levels.push(next);
    }
    levels
}

/// Full-tree root from leaf hashes (the reference implementation the
/// incremental updater must agree with).
pub fn rebuild_root(leaves: &[Hash32]) -> Hash32 {
    if leaves.is_empty() {
        return empty_root();
    }
    let levels = build_levels(leaves);
    levels.last().unwrap()[0]
}

/// One hash in a range proof, positioned canonically in the tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProofNode {
    pub level: usize,
    pub index: usize,
    pub hash: Hash32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RangeProof {
    /// Total number of leaves in the tree.
    pub n: usize,
    /// Requested half-open range of leaf indices.
    pub start: usize,
    pub end: usize,
    /// Ordered: range leaves first (level 0, ascending index), then siblings
    /// level by level from the bottom, left sibling before right sibling.
    pub nodes: Vec<ProofNode>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ProofError {
    /// start >= end on a non-empty tree.
    EmptyRange,
    /// Range or node falls outside the declared tree.
    OutOfRange,
    /// n==0 but range was not exactly [0,0).
    BadEmptyRange,
    /// Proof ended before the root was reconstructed.
    ProofTooShort,
    /// Proof carried nodes that reconstruction did not consume.
    ProofTooLong,
    /// A proof node appeared at a different (level,index) than required.
    MisalignedNode {
        expected_level: usize,
        expected_index: usize,
        got_level: usize,
        got_index: usize,
    },
    /// Recomputed leaf hash does not match the proof (block bytes tampered).
    LeafMismatch { index: usize },
    /// Declared data length disagrees with the declared leaf count.
    LengthMismatch,
    /// Reconstructed root differs from the trusted root.
    RootMismatch,
    /// Empty file proven against a non-empty (or wrong) root.
    EmptyRootMismatch,
}

impl std::fmt::Display for ProofError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProofError::EmptyRange => write!(f, "empty proof range [start,end)"),
            ProofError::OutOfRange => write!(f, "proof range exceeds declared leaf count"),
            ProofError::BadEmptyRange => write!(f, "zero-leaf tree requires range [0,0)"),
            ProofError::ProofTooShort => write!(f, "proof truncated before reaching the root"),
            ProofError::ProofTooLong => write!(f, "proof contains unconsumed nodes"),
            ProofError::MisalignedNode {
                expected_level,
                expected_index,
                got_level,
                got_index,
            } => {
                write!(
                    f,
                    "misaligned proof node: expected node ({expected_level},{expected_index}), got ({got_level},{got_index})"
                )
            }
            ProofError::LeafMismatch { index } => {
                write!(f, "leaf {index} hash mismatch with supplied block bytes")
            }
            ProofError::LengthMismatch => {
                write!(
                    f,
                    "declared byte length disagrees with leaf count and block size"
                )
            }
            ProofError::RootMismatch => write!(f, "reconstructed root does not match trusted root"),
            ProofError::EmptyRootMismatch => {
                write!(f, "zero-leaf tree root does not match empty-root sentinel")
            }
        }
    }
}

impl std::error::Error for ProofError {}

fn validate_range(n: usize, start: usize, end: usize) -> Result<(), ProofError> {
    if n == 0 {
        if start != 0 || end != 0 {
            return Err(ProofError::BadEmptyRange);
        }
        return Ok(());
    }
    if start >= end {
        return Err(ProofError::EmptyRange);
    }
    if start > end || end > n {
        return Err(ProofError::OutOfRange);
    }
    Ok(())
}

/// Generate a range proof, sourcing leaf and internal hashes via closures.
///
/// `node(level, index)` returns the canonical node stored at that position;
/// level 0 reads leaves.
pub fn prove<F>(n: usize, start: usize, end: usize, node: F) -> Result<RangeProof, ProofError>
where
    F: Fn(usize, usize) -> Hash32,
{
    validate_range(n, start, end)?;
    let mut nodes = Vec::new();
    if n == 0 {
        return Ok(RangeProof {
            n,
            start,
            end,
            nodes,
        });
    }

    let mut lo = start;
    let mut hi = end;
    let mut count = n;
    let mut level = 0usize;
    loop {
        // Known nodes at this level: exactly the range [lo, hi). At level 0
        // these are leaves, which we always include so the verifier can bind
        // them to the supplied block bytes.
        if level == 0 {
            for i in lo..hi {
                nodes.push(ProofNode {
                    level,
                    index: i,
                    hash: node(level, i),
                });
            }
        }
        if count == 1 {
            break;
        }
        // Left sibling: lo is odd, so it is the right half of a pair.
        if lo % 2 == 1 {
            nodes.push(ProofNode {
                level,
                index: lo - 1,
                hash: node(level, lo - 1),
            });
        }
        // Right sibling: (hi-1) is even and has a partner still in the level.
        if hi % 2 == 1 && hi < count {
            nodes.push(ProofNode {
                level,
                index: hi,
                hash: node(level, hi),
            });
        }
        lo /= 2;
        hi = hi.div_ceil(2);
        count = count.div_ceil(2);
        level += 1;
    }
    Ok(RangeProof {
        n,
        start,
        end,
        nodes,
    })
}

/// Verify a range proof against a trusted root and the actual block bytes.
///
/// `blocks` holds the bytes of exactly blocks `[start, end)`, in order. The
/// verifier recomputes leaf hashes itself; any length or content forgery in
/// the blocks is detected here.
pub fn verify(
    proof: &RangeProof,
    block_size: u64,
    data_len: u64,
    expected_root: &Hash32,
    blocks: &[Vec<u8>],
) -> Result<(), ProofError> {
    let RangeProof {
        n,
        start,
        end,
        nodes,
    } = proof;
    let (n, start, end) = (*n, *start, *end);

    // Length binding: the declared byte count must encode exactly n blocks.
    let expect_n = if data_len == 0 {
        0
    } else {
        ((data_len - 1) / block_size + 1) as usize
    };
    if expect_n != n {
        return Err(ProofError::LengthMismatch);
    }
    validate_range(n, start, end)?;
    if blocks.len() != end - start {
        return Err(ProofError::LengthMismatch);
    }

    // Empty file: the only valid proof is the empty one against the sentinel.
    if n == 0 {
        if !nodes.is_empty() {
            return Err(ProofError::ProofTooLong);
        }
        if expected_root != &empty_root() {
            return Err(ProofError::EmptyRootMismatch);
        }
        return Ok(());
    }

    let mut cursor = 0usize;
    let take = |cursor: &mut usize| -> Result<&ProofNode, ProofError> {
        let node = nodes.get(*cursor).ok_or(ProofError::ProofTooShort)?;
        *cursor += 1;
        Ok(node)
    };
    let expect_tag = |node: &ProofNode, level: usize, index: usize| -> Result<(), ProofError> {
        if node.level != level || node.index != index {
            return Err(ProofError::MisalignedNode {
                expected_level: level,
                expected_index: index,
                got_level: node.level,
                got_index: node.index,
            });
        }
        Ok(())
    };

    // 1. Leaves: recompute from block bytes and bind to the provided nodes.
    let mut cur: Vec<Hash32> = Vec::with_capacity(end - start);
    for (k, bytes) in blocks.iter().enumerate() {
        let i = start + k;
        let want_len = if i + 1 == n {
            (data_len - (i as u64) * block_size) as usize
        } else {
            block_size as usize
        };
        if bytes.len() != want_len {
            return Err(ProofError::LengthMismatch);
        }
        let node = take(&mut cursor)?;
        expect_tag(node, 0, i)?;
        let recomputed = leaf_hash(i, bytes);
        if recomputed != node.hash {
            return Err(ProofError::LeafMismatch { index: i });
        }
        cur.push(recomputed);
    }

    // 2. Climb level by level, consuming exactly the required siblings.
    let mut lo = start;
    let mut hi = end;
    let mut count = n;
    let mut level = 0usize;
    while count > 1 {
        if lo % 2 == 1 {
            let node = take(&mut cursor)?;
            expect_tag(node, level, lo - 1)?;
            cur.insert(0, node.hash);
            lo -= 1;
        }
        if hi % 2 == 1 && hi < count {
            let node = take(&mut cursor)?;
            expect_tag(node, level, hi)?;
            cur.push(node.hash);
            hi += 1;
        }
        // Pair everything up at this level (range is now even-aligned and
        // covers an even number of slots; an odd trailing node of the whole
        // tree only ever appears inside cur via promotion below).
        let mut next = Vec::with_capacity(cur.len().div_ceil(2));
        let mut i = 0;
        while i + 1 < cur.len() {
            next.push(internal_hash(&cur[i], &cur[i + 1]));
            i += 2;
        }
        if i < cur.len() {
            // Odd trailing node promoted unchanged — only possible when this
            // level itself had odd count.
            next.push(cur[i]);
        }
        cur = next;
        lo /= 2;
        hi = hi.div_ceil(2);
        count = count.div_ceil(2);
        level += 1;
    }

    // 3. Exactly one root node must remain.
    if cur.len() != 1 {
        return Err(ProofError::ProofTooShort);
    }
    if cursor != nodes.len() {
        return Err(ProofError::ProofTooLong);
    }
    if cur[0] != *expected_root {
        return Err(ProofError::RootMismatch);
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// JSON (de)serialization of proofs.
// ---------------------------------------------------------------------------

use crate::base64;
use crate::json::{self, Json};

pub fn proof_to_json(p: &RangeProof) -> Json {
    Json::Array(
        p.nodes
            .iter()
            .map(|nd| {
                json::obj(vec![
                    ("level", Json::Int(nd.level as i64)),
                    ("index", Json::Int(nd.index as i64)),
                    ("hash", Json::Str(base64::encode(&nd.hash))),
                ])
            })
            .collect(),
    )
}

pub fn proof_from_json(
    n: usize,
    start: usize,
    end: usize,
    value: &Json,
) -> Result<RangeProof, String> {
    let arr = value.as_array().ok_or("proof nodes must be an array")?;
    let mut nodes = Vec::with_capacity(arr.len());
    for item in arr {
        let level = item
            .get("level")
            .and_then(Json::as_i64)
            .ok_or("node.level missing")?;
        let index = item
            .get("index")
            .and_then(Json::as_i64)
            .ok_or("node.index missing")?;
        let hash_s = item
            .get("hash")
            .and_then(Json::as_str)
            .ok_or("node.hash missing")?;
        let bytes = base64::decode(hash_s).map_err(|_| "node.hash not valid base64")?;
        if bytes.len() != 32 || level < 0 || index < 0 {
            return Err("node.hash must decode to 32 bytes".into());
        }
        let mut hash = [0u8; 32];
        hash.copy_from_slice(&bytes);
        nodes.push(ProofNode {
            level: level as usize,
            index: index as usize,
            hash,
        });
    }
    Ok(RangeProof {
        n,
        start,
        end,
        nodes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blocks(data: &[u8], size: u64) -> Vec<Vec<u8>> {
        data.chunks(size as usize).map(|c| c.to_vec()).collect()
    }

    fn leaves_of(bs: &[Vec<u8>]) -> Vec<Hash32> {
        bs.iter()
            .enumerate()
            .map(|(i, b)| leaf_hash(i, b))
            .collect()
    }

    #[test]
    fn leaf_and_internal_domains_differ() {
        // A leaf hash can never be confused with an internal hash.
        let h = leaf_hash(0, &[1, 32]);
        let internal = internal_hash(&h, &h);
        assert_ne!(h, internal);
        assert_ne!(h, empty_root());
        assert_ne!(internal, empty_root());
    }

    #[test]
    fn empty_file_root_is_sentinel() {
        assert_eq!(rebuild_root(&[]), empty_root());
    }

    #[test]
    fn odd_rule_does_not_duplicate() {
        // For 3 leaves the promoted leaf must appear in level 1 verbatim.
        let l = vec![leaf_hash(0, b"a"), leaf_hash(1, b"b"), leaf_hash(2, b"c")];
        let levels = build_levels(&l);
        assert_eq!(levels.len(), 3);
        assert_eq!(levels[1].len(), 2);
        assert_eq!(levels[1][1], l[2]); // promoted, not hash(c,c)
        assert_eq!(levels[1][0], internal_hash(&l[0], &l[1]));
        assert_eq!(levels[2].len(), 1);
        assert_eq!(levels[2][0], internal_hash(&levels[1][0], &levels[1][1]));
    }

    #[test]
    fn all_ranges_verify_for_many_sizes() {
        for size in 1u64..=6 {
            for nblocks in 0usize..12 {
                let data: Vec<u8> = (0..(nblocks as u64 * size))
                    .map(|i| (i % 251) as u8)
                    .collect();
                let bs = blocks(&data, size);
                assert_eq!(bs.len(), nblocks);
                let leaves = leaves_of(&bs);
                let root = rebuild_root(&leaves);
                let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];

                if nblocks == 0 {
                    let p = prove(0, 0, 0, node).unwrap();
                    assert!(p.nodes.is_empty());
                    verify(&p, size, 0, &root, &[]).unwrap();
                    continue;
                }
                for start in 0..nblocks {
                    for end in start + 1..=nblocks {
                        let p = prove(nblocks, start, end, node).unwrap();
                        let slice = bs[start..end].to_vec();
                        verify(&p, size, data.len() as u64, &root, &slice).unwrap_or_else(|e| {
                            panic!("n={nblocks} range {start}..{end} size {size}: {e}")
                        });
                        // Proof size never exceeds range + 2*height.
                        let mut h = 1;
                        let mut c = nblocks;
                        while c > 1 {
                            c = c.div_ceil(2);
                            h += 1;
                        }
                        assert!(p.nodes.len() <= (end - start) + 2 * (h - 1));
                    }
                }
            }
        }
    }

    #[test]
    fn full_range_needs_no_siblings() {
        let bs = blocks(b"abcdefgh", 2);
        let leaves = leaves_of(&bs);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        let p = prove(4, 0, 4, node).unwrap();
        assert_eq!(p.nodes.len(), 4);
        assert!(p.nodes.iter().all(|nd| nd.level == 0));
    }

    #[test]
    fn first_and_last_single_block_ranges() {
        let bs = blocks(b"abcde", 2); // 3 blocks: ab cd e
        let leaves = leaves_of(&bs);
        let root = rebuild_root(&leaves);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        // First block.
        let p0 = prove(3, 0, 1, node).unwrap();
        verify(&p0, 2, 5, &root, &[b"ab".to_vec()]).unwrap();
        // Last (short) block.
        let p2 = prove(3, 2, 3, node).unwrap();
        verify(&p2, 2, 5, &root, &[b"e".to_vec()]).unwrap();
    }

    #[test]
    fn tampered_block_detected() {
        let bs = blocks(b"abcde", 2);
        let leaves = leaves_of(&bs);
        let root = rebuild_root(&leaves);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        let p = prove(3, 0, 1, node).unwrap();
        let err = verify(&p, 2, 5, &root, &[b"XB".to_vec()]).unwrap_err();
        assert_eq!(err, ProofError::LeafMismatch { index: 0 });
    }

    #[test]
    fn forged_lengths_detected() {
        let bs = blocks(b"abcde", 2);
        let leaves = leaves_of(&bs);
        let root = rebuild_root(&leaves);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        let p = prove(3, 2, 3, node).unwrap();

        // Claim a longer file: last block padded to full size. The declared
        // length is still consistent with n=3, so the padding forgery is
        // caught at the leaf hash (block bytes do not match the commitment).
        assert!(matches!(
            verify(&p, 2, 6, &root, &[b"e\0".to_vec()]).unwrap_err(),
            ProofError::LeafMismatch { index: 2 }
        ));
        // Claim a data_len consistent with a DIFFERENT leaf count.
        assert_eq!(
            verify(&p, 2, 7, &root, &[b"e".to_vec()]).unwrap_err(),
            ProofError::LengthMismatch
        );
        // Claim a shorter file: computed n disagrees with proof.n.
        assert_eq!(
            verify(&p, 2, 4, &root, &[b"e".to_vec()]).unwrap_err(),
            ProofError::LengthMismatch
        );
        // Right length, wrong trusted root.
        let wrong = [0u8; 32];
        assert_eq!(
            verify(&p, 2, 5, &wrong, &[b"e".to_vec()]).unwrap_err(),
            ProofError::RootMismatch
        );
    }

    #[test]
    fn misaligned_and_resized_proofs_detected() {
        let bs = blocks(b"abcdefgh", 2);
        let leaves = leaves_of(&bs);
        let root = rebuild_root(&leaves);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        let good = prove(4, 1, 2, node).unwrap();
        let data = vec![b"cd".to_vec()];

        // Swap two proof nodes.
        let mut swapped = good.clone();
        swapped.nodes.swap(1, 2);
        assert!(matches!(
            verify(&swapped, 2, 8, &root, &data).unwrap_err(),
            ProofError::MisalignedNode { .. }
        ));

        // Drop a node.
        let mut truncated = good.clone();
        truncated.nodes.pop();
        assert_eq!(
            verify(&truncated, 2, 8, &root, &data).unwrap_err(),
            ProofError::ProofTooShort
        );

        // Append a bogus node.
        let mut extended = good.clone();
        extended.nodes.push(ProofNode {
            level: 9,
            index: 9,
            hash: [7u8; 32],
        });
        assert_eq!(
            verify(&extended, 2, 8, &root, &data).unwrap_err(),
            ProofError::ProofTooLong
        );

        // Forge a tag without touching hashes.
        let mut retagged = good.clone();
        retagged.nodes[1].index = 2;
        assert!(matches!(
            verify(&retagged, 2, 8, &root, &data).unwrap_err(),
            ProofError::MisalignedNode { .. }
        ));

        // Range outside the tree.
        let bad = prove(4, 0, 5, node).unwrap_err();
        assert_eq!(bad, ProofError::OutOfRange);
    }

    #[test]
    fn empty_range_rejected_on_nonempty_tree() {
        let bs = blocks(b"ab", 2);
        let leaves = leaves_of(&bs);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        assert_eq!(prove(1, 0, 0, node).unwrap_err(), ProofError::EmptyRange);
    }

    #[test]
    fn proof_json_roundtrip() {
        let bs = blocks(b"abcde", 2);
        let leaves = leaves_of(&bs);
        let node = |lvl: usize, idx: usize| build_levels(&leaves)[lvl][idx];
        let p = prove(3, 0, 2, node).unwrap();
        let j = proof_to_json(&p);
        let p2 = proof_from_json(3, 0, 2, &j).unwrap();
        assert_eq!(p, p2);
    }
}
