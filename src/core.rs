//! Service-side Merkle core: domain-separated hashing, tree construction and
//! proof generation.
//!
//! # Domain-separated preimages (all hashing is SHA-256)
//!
//! | node   | preimage                                                       |
//! |--------|----------------------------------------------------------------|
//! | leaf   | `0x00 ‖ u32be(key.len) ‖ key ‖ u32be(val.len) ‖ val`           |
//! | inner  | `0x01 ‖ left_hash(32) ‖ right_hash(32)`                        |
//! | commit | `0x02 ‖ u64be(version) ‖ top_hash(32) ‖ u64be(n) ‖ u32be(h)`  |
//!
//! The leading tag byte prevents leaf/inner/commit preimage collisions. The
//! length-prefixed leaf framing prevents key/value concatenation ambiguity.
//!
//! Leaves are ordered by the **raw byte-wise lexicographic order of keys**
//! (RocksDB's default comparator). An even node paired with an odd node is a
//! binary inner node; an odd node at the end of a level is *promoted* unchanged
//! (re-hashed as nothing). The tree top for an empty tree is `[0u8; 32]`; for
//! a single leaf it is that leaf's hash.
//!
//! The independent verifier in [`crate::verifier`] re-derives every byte of
//! this specification from scratch — it does not call anything in this module.

use sha2::{Digest, Sha256};

pub const TAG_LEAF: u8 = 0x00;
pub const TAG_INNER: u8 = 0x01;
pub const TAG_COMMIT: u8 = 0x02;

/// Top hash of the empty tree (also the "zero subtree" sentinel).
pub const EMPTY_TOP: [u8; 32] = [0u8; 32];

fn u32be(v: usize) -> [u8; 4] {
    (v as u32).to_be_bytes()
}

fn sha256(data: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize().into()
}

/// Hash a leaf. An empty value is a legitimate present value (`val = []`);
/// absence is *never* represented at this layer.
pub fn leaf_hash(key: &[u8], value: &[u8]) -> [u8; 32] {
    let mut pre = Vec::with_capacity(1 + 4 + key.len() + 4 + value.len());
    pre.push(TAG_LEAF);
    pre.extend_from_slice(&u32be(key.len()));
    pre.extend_from_slice(key);
    pre.extend_from_slice(&u32be(value.len()));
    pre.extend_from_slice(value);
    sha256(&pre)
}

/// Hash an internal node from exactly two 32-byte child hashes.
pub fn inner_hash(left: &[u8; 32], right: &[u8; 32]) -> [u8; 32] {
    let mut pre = [0u8; 1 + 64];
    pre[0] = TAG_INNER;
    pre[1..33].copy_from_slice(left);
    pre[33..65].copy_from_slice(right);
    sha256(&pre)
}

/// Hash a version root commit, binding version, tree top, leaf count and height.
pub fn commit_hash(version: u64, top: &[u8; 32], leaf_count: u64, height: u32) -> [u8; 32] {
    let mut pre = Vec::with_capacity(1 + 8 + 32 + 8 + 4);
    pre.push(TAG_COMMIT);
    pre.extend_from_slice(&version.to_be_bytes());
    pre.extend_from_slice(top);
    pre.extend_from_slice(&leaf_count.to_be_bytes());
    pre.extend_from_slice(&height.to_be_bytes());
    sha256(&pre)
}

/// Number of promotion rounds above `count` leaves: `0` for 0 or 1 leaf.
pub fn tree_height(count: u64) -> u32 {
    let mut h = 0u32;
    let mut c = count;
    while c > 1 {
        c = c.div_ceil(2);
        h += 1;
    }
    h
}

/// A path step as emitted by the prover.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PathEntry {
    /// Sibling is on the left; current node on the right.
    Left([u8; 32]),
    /// Sibling is on the right; current node on the left.
    Right([u8; 32]),
    /// No sibling: the current node was promoted unchanged at this level.
    Promoted,
}

/// A fully built, immutable version tree plus the node records to persist.
pub struct BuiltTree {
    /// Sorted `(key, value)` leaves. `value == []` means a present empty value.
    pub leaves: Vec<(Vec<u8>, Vec<u8>)>,
    /// `levels[0]` are the leaf hashes; each next level is parents.
    pub levels: Vec<Vec<[u8; 32]>>,
    /// Node records to persist: `hash -> preimage bytes` (deduplicated).
    pub records: Vec<([u8; 32], Vec<u8>)>,
    pub top: [u8; 32],
    pub leaf_count: u64,
    pub height: u32,
}

/// Build the tree from sorted, unique leaves.
///
/// Caller MUST sort by raw key bytes and deduplicate. `store::Store` does.
pub fn build_tree(leaves: Vec<(Vec<u8>, Vec<u8>)>) -> BuiltTree {
    let leaf_count = leaves.len() as u64;
    let height = tree_height(leaf_count);

    // Node preimages are persisted once each (content-addressed, dedup across
    // versions); `seen` tracks which hashes already have a record.
    let mut seen: std::collections::HashSet<[u8; 32]> = std::collections::HashSet::new();
    let mut records: Vec<([u8; 32], Vec<u8>)> = Vec::new();

    let mut leaf_hashes = Vec::with_capacity(leaves.len());
    for (k, v) in &leaves {
        let h = leaf_hash(k, v);
        leaf_hashes.push(h);
        if seen.insert(h) {
            let mut rec = Vec::with_capacity(1 + 4 + k.len() + 4 + v.len());
            rec.push(TAG_LEAF);
            rec.extend_from_slice(&u32be(k.len()));
            rec.extend_from_slice(k);
            rec.extend_from_slice(&u32be(v.len()));
            rec.extend_from_slice(v);
            records.push((h, rec));
        }
    }

    let mut levels: Vec<Vec<[u8; 32]>> = Vec::new();
    levels.push(leaf_hashes);

    while levels.last().unwrap().len() > 1 {
        let cur = levels.last().unwrap();
        let mut next: Vec<[u8; 32]> = Vec::with_capacity(cur.len().div_ceil(2));
        let mut i = 0;
        while i < cur.len() {
            if i + 1 < cur.len() {
                let p = inner_hash(&cur[i], &cur[i + 1]);
                next.push(p);
                if seen.insert(p) {
                    let mut rec = Vec::with_capacity(65);
                    rec.push(TAG_INNER);
                    rec.extend_from_slice(&cur[i]);
                    rec.extend_from_slice(&cur[i + 1]);
                    records.push((p, rec));
                }
            } else {
                // Odd trailing node: promote unchanged (no new hash/record).
                next.push(cur[i]);
            }
            i += 2;
        }
        levels.push(next);
    }

    let top = if leaf_count == 0 {
        EMPTY_TOP
    } else {
        levels.last().unwrap()[0]
    };

    BuiltTree {
        leaves,
        levels,
        records,
        top,
        leaf_count,
        height,
    }
}

/// Generate the bottom-up Merkle path for leaf `index` in a built tree.
/// Returns one entry per level (`height` entries; empty for a 0/1-leaf tree).
pub fn prove_index(tree: &BuiltTree, index: u64) -> Vec<PathEntry> {
    assert!(index < tree.leaf_count, "leaf index out of range");
    let mut path = Vec::with_capacity(tree.height as usize);
    let mut pos = index as usize;
    let mut count = tree.leaf_count as usize;
    for level in 0..tree.height as usize {
        let nodes = &tree.levels[level];
        let entry = if pos.is_multiple_of(2) {
            if pos + 1 < count {
                PathEntry::Right(nodes[pos + 1])
            } else {
                PathEntry::Promoted
            }
        } else {
            PathEntry::Left(nodes[pos - 1])
        };
        path.push(entry);
        pos /= 2;
        count = count.div_ceil(2);
    }
    path
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kv(s: &str, v: &str) -> (Vec<u8>, Vec<u8>) {
        (s.as_bytes().to_vec(), v.as_bytes().to_vec())
    }

    #[test]
    fn empty_and_single_tree_tops() {
        let t = build_tree(vec![]);
        assert_eq!(t.top, EMPTY_TOP);
        assert_eq!(t.height, 0);
        assert_eq!(t.leaf_count, 0);
        assert!(prove_empty(&t));

        let one = build_tree(vec![kv("a", "1")]);
        assert_eq!(one.top, leaf_hash(b"a", b"1"));
        assert_eq!(one.height, 0);
        assert!(prove_index(&one, 0).is_empty());
    }

    fn prove_empty(t: &BuiltTree) -> bool {
        t.leaf_count == 0 && t.top == EMPTY_TOP
    }

    #[test]
    fn root_is_order_sensitive_and_domain_separated() {
        let t1 = build_tree(vec![kv("a", "1"), kv("b", "2")]);
        let t2 = build_tree(vec![kv("a", "2"), kv("b", "1")]);
        assert_ne!(t1.top, t2.top);
        // A leaf preimage must never be usable as an inner-node preimage.
        let l = leaf_hash(b"a", b"1");
        let as_inner = {
            let mut pre = vec![TAG_INNER];
            pre.extend_from_slice(&[0u8; 32]);
            pre.extend_from_slice(&l); // arbitrary 32 bytes
            sha256(&pre)
        };
        assert_ne!(as_inner, l);
    }
}
