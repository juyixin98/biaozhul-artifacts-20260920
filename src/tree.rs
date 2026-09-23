//! Sorted-key Merkle tree construction and proof generation.
//!
//! The tree is built from a fully materialised sorted snapshot of the KV
//! state. Leaves are sorted by raw byte order; at every internal level the
//! last node of an odd-width level is promoted by hashing it with itself
//! (`H(n, n)`), Bitcoin-style. Directions in inclusion paths follow from the
//! leaf index and level widths, never from stored per-node metadata, so an
//! independent verifier can reconstruct everything from the proof alone.

use crate::error::{Error, Result};
use crate::hash::{empty_root, hash_branch, hash_leaf, Hash};
use crate::proof::{
    Bound, Entry, InclusionProof, NonExistenceProof, ProofResponse, ProofStep, Side,
};
use crate::encoding::{Hex32, HexBytes};

/// A stored key/value pair. An empty value is a legitimate value; key
/// absence is represented by the key not appearing in the snapshot at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KV {
    pub key: Vec<u8>,
    pub value: Vec<u8>,
}

/// Immutable view of one version: sorted entries plus every tree level.
///
/// Level 0 holds leaf hashes; level `i+1` pairs level `i`. Level `depth`
/// contains exactly one hash, the root. An empty snapshot has no levels and
/// its root is [`empty_root`].
#[derive(Debug)]
pub struct SnapshotTree {
    entries: Vec<KV>,
    levels: Vec<Vec<Hash>>,
    root: Hash,
}

impl SnapshotTree {
    /// Build the tree. `entries` must already be sorted and unique by key.
    pub fn from_sorted(entries: Vec<KV>) -> Result<Self> {
        if entries.is_empty() {
            return Ok(SnapshotTree { entries, levels: Vec::new(), root: empty_root() });
        }
        for w in entries.windows(2) {
            if w[0].key >= w[1].key {
                return Err(Error::Storage(format!(
                    "snapshot entries not strictly sorted: {:?} before {:?}",
                    w[0].key, w[1].key
                )));
            }
        }

        let mut levels: Vec<Vec<Hash>> = Vec::new();
        let leaf_level: Vec<Hash> = entries
            .iter()
            .map(|kv| hash_leaf(&kv.key, &kv.value))
            .collect();
        levels.push(leaf_level);

        loop {
            let cur = levels.last().unwrap();
            if cur.len() == 1 {
                break;
            }
            let mut next = Vec::with_capacity(cur.len().div_ceil(2));
            let mut i = 0;
            while i < cur.len() {
                let node = if i + 1 < cur.len() {
                    hash_branch(&cur[i], &cur[i + 1])
                } else {
                    // Odd last node: duplicate it for pairing.
                    hash_branch(&cur[i], &cur[i])
                };
                next.push(node);
                i += 2;
            }
            levels.push(next);
        }

        let root = *levels.last().unwrap()[0..].first().unwrap();
        Ok(SnapshotTree { entries, levels, root })
    }

    pub fn root(&self) -> &Hash {
        &self.root
    }

    pub fn leaf_count(&self) -> u64 {
        self.entries.len() as u64
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn entries(&self) -> &[KV] {
        &self.entries
    }

    fn widths(&self) -> Vec<u64> {
        self.levels.iter().map(|l| l.len() as u64).collect()
    }

    /// Build the inclusion path (bottom-up) for leaf `index`.
    fn path_for(&self, index: usize) -> Vec<ProofStep> {
        let widths = self.widths();
        let depth = self.levels.len() - 1;
        let mut steps = Vec::with_capacity(depth);
        let mut slot = index;
        for (level_hashes, &level_width) in self.levels.iter().zip(widths.iter()).take(depth) {
            let width = level_width as usize;
            let step = if slot.is_multiple_of(2) {
                // Path node is the LEFT child; sibling is to the right.
                let sib = if slot + 1 < width {
                    level_hashes[slot + 1]
                } else {
                    // Duplicated odd last node: sibling hash == own hash.
                    level_hashes[slot]
                };
                ProofStep { sibling_hash: Hex32(sib), side: Side::Right }
            } else {
                // Path node is the RIGHT child; sibling to the left.
                ProofStep {
                    sibling_hash: Hex32(level_hashes[slot - 1]),
                    side: Side::Left,
                }
            };
            steps.push(step);
            slot /= 2;
        }
        steps
    }

    fn inclusion(&self, version: u64, index: usize) -> InclusionProof {
        let kv = &self.entries[index];
        InclusionProof {
            version,
            root: Hex32(self.root),
            leaf_count: self.entries.len() as u64,
            index: index as u64,
            entry: Entry {
                key: HexBytes(kv.key.clone()),
                value: HexBytes(kv.value.clone()),
            },
            path: self.path_for(index),
        }
    }

    /// Answer a key query against this version: either an inclusion proof
    /// with the value, or a neighbor-bounded non-existence proof.
    pub fn prove(&self, version: u64, key: &[u8]) -> ProofResponse {
        match self.entries.binary_search_by(|kv| kv.key.as_slice().cmp(key)) {
            Ok(i) => {
                let proof = self.inclusion(version, i);
                ProofResponse::Exists {
                    exists: true,
                    key: HexBytes(key.to_vec()),
                    value: HexBytes(self.entries[i].value.clone()),
                    proof,
                }
            }
            Err(_) if self.is_empty() => ProofResponse::Missing {
                exists: false,
                key: HexBytes(key.to_vec()),
                proof: NonExistenceProof {
                    version,
                    root: Hex32(self.root),
                    queried_key: HexBytes(key.to_vec()),
                    empty_tree: true,
                    bounds: Vec::new(),
                },
            },
            Err(insert_at) => {
                // Neighbors by sorted position around the insertion point.
                let right_idx = if insert_at < self.entries.len() { Some(insert_at) } else { None };
                let left_idx = insert_at.checked_sub(1);

                let mut bounds = Vec::with_capacity(2);
                if let Some(i) = left_idx {
                    bounds.push(Bound { side: Side::Left, proof: self.inclusion(version, i) });
                }
                if let Some(i) = right_idx {
                    bounds.push(Bound { side: Side::Right, proof: self.inclusion(version, i) });
                }
                ProofResponse::Missing {
                    exists: false,
                    key: HexBytes(key.to_vec()),
                    proof: NonExistenceProof {
                        version,
                        root: Hex32(self.root),
                        queried_key: HexBytes(key.to_vec()),
                        empty_tree: false,
                        bounds,
                    },
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kvs(pairs: &[(&str, &str)]) -> Vec<KV> {
        let mut v: Vec<KV> = pairs
            .iter()
            .map(|(k, val)| KV { key: k.as_bytes().to_vec(), value: val.as_bytes().to_vec() })
            .collect();
        v.sort_by(|a, b| a.key.cmp(&b.key));
        v.dedup_by(|a, b| a.key == b.key);
        v
    }

    #[test]
    fn builds_shapes_and_roots_stable() {
        let e = SnapshotTree::from_sorted(vec![]).unwrap();
        assert_eq!(e.root(), &empty_root());

        let one = SnapshotTree::from_sorted(kvs(&[("a", "1")])).unwrap();
        // Single leaf: root IS the leaf hash, no levels of pairing.
        assert_eq!(one.root(), &hash_leaf(b"a", b"1"));

        let two = SnapshotTree::from_sorted(kvs(&[("a", "1"), ("b", "2")])).unwrap();
        assert_eq!(
            two.root(),
            &hash_branch(&hash_leaf(b"a", b"1"), &hash_leaf(b"b", b"2"))
        );

        // Odd count duplicates the last node at level 0.
        let three = SnapshotTree::from_sorted(kvs(&[("a", "1"), ("b", "2"), ("c", "3")])).unwrap();
        let ha = hash_leaf(b"a", b"1");
        let hb = hash_leaf(b"b", b"2");
        let hc = hash_leaf(b"c", b"3");
        let l = hash_branch(&ha, &hb);
        let r = hash_branch(&hc, &hc);
        assert_eq!(three.root(), &hash_branch(&l, &r));
    }

    #[test]
    fn rejects_unsorted_input() {
        let bad = vec![
            KV { key: b"b".to_vec(), value: vec![] },
            KV { key: b"a".to_vec(), value: vec![] },
        ];
        assert!(SnapshotTree::from_sorted(bad).is_err());
    }
}
