//! Proof protocol types and the storage-independent verifier.
//!
//! The verifier in this module takes only a root hash and a proof — it never
//! touches RocksDB or the service state, exactly like an external relying
//! party would. Proof generation lives in [`crate::tree`].

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::encoding::{Hex32, HexBytes};
use crate::error::{Error, Result};
use crate::hash::{empty_root, hash_branch, hash_leaf};

/// Which side a proof sibling sits on relative to the path node.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Side {
    /// Sibling is the LEFT child; the path node is the right child.
    Left,
    /// Sibling is the RIGHT child; the path node is the left child.
    Right,
}

/// One internal-level step of an inclusion proof.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProofStep {
    /// Hash of the sibling subtree at this level.
    pub sibling_hash: Hex32,
    /// Where the sibling sits relative to the path node.
    pub side: Side,
}

/// The leaf an inclusion path is anchored to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Entry {
    pub key: HexBytes,
    pub value: HexBytes,
}

/// A Merkle inclusion proof for one leaf.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InclusionProof {
    pub version: u64,
    pub root: Hex32,
    /// Number of leaves in this version's tree.
    pub leaf_count: u64,
    /// Zero-based position of the proven leaf in key-sorted order.
    pub index: u64,
    pub entry: Entry,
    pub path: Vec<ProofStep>,
}

/// One bound of a non-existence proof: an inclusion proof of the adjacent
/// existing leaf, plus the side on which that neighbor sits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bound {
    /// `left`  → `neighbor.key < queried key` (greatest key below),
    /// `right` → `neighbor.key > queried key` (smallest key above).
    pub side: Side,
    pub proof: InclusionProof,
}

/// Proof that a key does not exist in a (possibly empty) version.
///
/// Non-existence is NEVER represented by an empty value. It is witnessed by
/// the closest existing neighbor(s): one bound at an edge, two in the middle.
/// An empty tree carries neither.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NonExistenceProof {
    pub version: u64,
    pub root: Hex32,
    pub queried_key: HexBytes,
    /// `true` when the version is the empty tree (no neighbors available).
    pub empty_tree: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bounds: Vec<Bound>,
}

/// Server response / standalone verify input for a proof query.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ProofResponse {
    /// Key exists: value + inclusion proof.
    Exists {
        exists: bool,
        key: HexBytes,
        value: HexBytes,
        proof: InclusionProof,
    },
    /// Key absent: bounded non-existence proof.
    Missing {
        exists: bool,
        key: HexBytes,
        proof: NonExistenceProof,
    },
}

// ---------------------------------------------------------------------------
// Pure verification
// ---------------------------------------------------------------------------

/// Recompute the root for an inclusion path.
///
/// `leaf_hash` is the starting hash (the leaf itself). `steps` go from the
/// leaf level upward; each step names the sibling and its position.
///
/// `leaf_count` is required: with a duplicated-odd-node tree the verifier
/// must be able to detect a step whose sibling slot points past the current
/// level (in which case the sibling hash must equal the running hash, which
/// is exactly how a duplicated node is represented).
fn fold_path(
    leaf_hash: &[u8; 32],
    steps: &[ProofStep],
    leaf_count: u64,
    index: u64,
) -> Result<[u8; 32]> {
    if leaf_count == 0 {
        return Err(Error::bad("inclusion proof against empty tree"));
    }

    // Width of each level, i.e. level 0 = leaf_count.
    let mut widths = Vec::with_capacity(steps.len() + 1);
    let mut w = leaf_count;
    widths.push(w);
    for _ in 0..steps.len() {
        w = w / 2 + w % 2;
        widths.push(w);
    }

    let mut running = *leaf_hash;
    // Slot of the running node at the current level.
    let mut slot = index;

    for (level, step) in steps.iter().enumerate() {
        let width = widths[level];
        let sib_slot = match step.side {
            Side::Left => slot.checked_sub(1).ok_or_else(|| {
                Error::bad("path node has no left neighbor at this level")
            })?,
            Side::Right => slot + 1,
        };

        if sib_slot >= width {
            // Sibling slot outside the level: the only legitimate way this
            // happens is an odd-width level whose last node was promoted by
            // duplication. Then the sibling must equal the running node.
            if sib_slot != width {
                return Err(Error::bad("sibling slot past level width"));
            }
            if step.sibling_hash.0 != running {
                return Err(Error::bad("duplicated-node sibling hash mismatch"));
            }
        }

        let (left, right) = match step.side {
            Side::Left => (&step.sibling_hash.0, &running),
            Side::Right => (&running, &step.sibling_hash.0),
        };
        running = hash_branch(left, right);
        slot /= 2;
    }

    // The final running node must be the sole node at the top level.
    if *widths.last().unwrap() != 1 {
        return Err(Error::bad("path length does not match leaf_count"));
    }
    Ok(running)
}

/// Verify an inclusion proof against `claimed_root` using only the proof
/// itself and the root. Returns `Ok(())` when the proof is valid.
pub fn verify_inclusion(proof: &InclusionProof, claimed_root: &[u8; 32]) -> Result<()> {
    if proof.leaf_count == 0 {
        return Err(Error::bad("inclusion proof against empty tree"));
    }
    if proof.index >= proof.leaf_count {
        return Err(Error::bad("index out of leaf_count range"));
    }

    // The side bits independently fix the leaf index: at level i the path
    // node is a right child exactly when its sibling is on the LEFT, so bit i
    // is 1 iff that step's side is Left.
    let mut derived_index = 0u64;
    for (i, step) in proof.path.iter().enumerate() {
        if matches!(step.side, Side::Left) {
            let bit = u64::try_from(i).ok().and_then(|i| 1u64.checked_shl(i as u32));
            let bit = bit.ok_or_else(|| Error::bad("path too deep"))?;
            derived_index |= bit;
        }
    }
    if derived_index != proof.index {
        return Err(Error::bad("proof index does not match path shape"));
    }

    // Expected number of levels follows solely from the leaf count.
    let mut w = proof.leaf_count;
    let mut expected_steps = 0u32;
    while w > 1 {
        w = w / 2 + w % 2;
        expected_steps += 1;
    }
    if proof.path.len() as u32 != expected_steps {
        return Err(Error::bad("path length does not match leaf_count"));
    }

    // Entry must be cryptographically bound to the leaf.
    let leaf = hash_leaf(proof.entry.key.as_bytes(), proof.entry.value.as_bytes());

    let root = fold_path(&leaf, &proof.path, proof.leaf_count, proof.index)?;

    if &root != claimed_root {
        return Err(Error::bad("root mismatch"));
    }
    if *proof.root.as_hash() != *claimed_root {
        return Err(Error::bad("embedded root disagrees with claimed root"));
    }
    Ok(())
}

fn verify_bound<'a>(bound: &'a Bound, root: &[u8; 32]) -> Result<&'a [u8]> {
    if *bound.proof.root.as_hash() != *root {
        return Err(Error::bad("bound proof targets a different root"));
    }
    verify_inclusion(&bound.proof, root)?;
    Ok(bound.proof.entry.key.as_bytes())
}

/// Verify a non-existence proof against `claimed_root`.
///
/// Checks, in order:
/// 1. every bound is a valid inclusion proof under `claimed_root`;
/// 2. the empty-tree marker matches when `empty_tree` is set (and no
///    neighbors are given), and vice versa;
/// 3. the neighbor keys actually bracket the queried key (strictly);
/// 4. two bounds are adjacent (consecutive leaf indices).
pub fn verify_non_existence(
    p: &NonExistenceProof,
    claimed_root: &[u8; 32],
) -> Result<()> {
    if *p.root.as_hash() != *claimed_root {
        return Err(Error::bad("embedded root disagrees with claimed root"));
    }

    let empty = empty_root();
    if p.empty_tree {
        if claimed_root != &empty {
            return Err(Error::bad("empty_tree asserted but root is not the empty root"));
        }
        if !p.bounds.is_empty() {
            return Err(Error::bad("empty tree proof must have no bounds"));
        }
        return Ok(());
    }
    if claimed_root == &empty {
        return Err(Error::bad("root is the empty root but empty_tree is not set"));
    }

    let q = p.queried_key.as_bytes();

    match p.bounds.len() {
        1 => {
            let b = &p.bounds[0];
            let k = verify_bound(b, claimed_root)?;
            match b.side {
                // Only the greatest key can witness a query above it.
                Side::Left => {
                    if k >= q {
                        return Err(Error::bad("left bound key not strictly smaller"));
                    }
                    if b.proof.index + 1 != b.proof.leaf_count {
                        return Err(Error::bad("left bound is not the rightmost leaf"));
                    }
                }
                // Only the smallest key can witness a query below it.
                Side::Right => {
                    if k <= q {
                        return Err(Error::bad("right bound key not strictly greater"));
                    }
                    if b.proof.index != 0 {
                        return Err(Error::bad("right bound is not the leftmost leaf"));
                    }
                }
            }
        }
        2 => {
            // Keep ordering independent of array order in the payload.
            let (lb, rb) = match (p.bounds[0].side, p.bounds[1].side) {
                (Side::Left, Side::Right) => (&p.bounds[0], &p.bounds[1]),
                (Side::Right, Side::Left) => (&p.bounds[1], &p.bounds[0]),
                _ => return Err(Error::bad("two bounds must be one left and one right")),
            };
            let lk = verify_bound(lb, claimed_root)?;
            let rk = verify_bound(rb, claimed_root)?;
            if !(lk < q && q < rk) {
                return Err(Error::bad("queried key is not strictly between bounds"));
            }
            // Neighbors must be adjacent leaves in sorted order.
            if lb.proof.leaf_count != rb.proof.leaf_count {
                return Err(Error::bad("bounds disagree on leaf_count"));
            }
            if lb.proof.index + 1 != rb.proof.index {
                return Err(Error::bad("bounds are not adjacent leaves"));
            }
        }
        n => return Err(Error::bad(format!("non-existence proof needs 1 or 2 bounds, got {n}"))),
    }
    Ok(())
}

/// Verify either kind of [`ProofResponse`] against a claimed root.
pub fn verify_response(resp: &ProofResponse, claimed_root: &[u8; 32]) -> Result<()> {
    match resp {
        ProofResponse::Exists { key, value, proof, .. } => {
            if proof.entry.key.0 != key.0 {
                return Err(Error::bad("response key differs from proof entry key"));
            }
            if proof.entry.value.0 != value.0 {
                return Err(Error::bad("response value differs from proof entry value"));
            }
            verify_inclusion(proof, claimed_root)
        }
        ProofResponse::Missing { key, proof, .. } => {
            if proof.queried_key.0 != key.0 {
                return Err(Error::bad("response key differs from proof queried key"));
            }
            verify_non_existence(proof, claimed_root)
        }
    }
}

/// Human-readable verification outcome for the `/v1/verify` endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Valid,
    Invalid,
}

impl fmt::Display for Verdict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Verdict::Valid => "valid",
            Verdict::Invalid => "invalid",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tree::{KV, SnapshotTree};

    fn ent(pairs: &[(&str, &str)]) -> Vec<KV> {
        pairs
            .iter()
            .map(|(k, v)| KV { key: k.as_bytes().to_vec(), value: v.as_bytes().to_vec() })
            .collect()
    }

    fn build(pairs: &[(&str, &str)]) -> SnapshotTree {
        SnapshotTree::from_sorted(ent(pairs)).unwrap()
    }

    #[test]
    fn inclusion_holds_for_all_shapes() {
        for n in 0usize..=12 {
            let pairs: Vec<(String, String)> =
                (0..n).map(|i| (format!("k{i:03}"), format!("v{i}"))).collect();
            let refs: Vec<(&str, &str)> =
                pairs.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
            let tree = build(&refs);
            for (i, (k, v)) in pairs.iter().enumerate() {
                let resp = tree.prove(1, k.as_bytes());
                match resp {
                    ProofResponse::Exists { value, proof, .. } => {
                        assert_eq!(value.0, v.as_bytes());
                        verify_inclusion(&proof, tree.root()).unwrap();
                        assert_eq!(proof.index as usize, i);
                    }
                    other => panic!("expected existence for {k}, got {other:?}"),
                }
            }
        }
    }

    #[test]
    fn non_existence_edges_and_middle() {
        let tree = build(&[("b", "1"), ("d", "2"), ("f", "3")]);
        for q in ["a", "c", "e", "g", "\x00", "zzz"] {
            let ProofResponse::Missing { proof, .. } = tree.prove(1, q.as_bytes()) else {
                panic!("{q} should be missing");
            };
            verify_non_existence(&proof, tree.root()).unwrap();
        }
    }

    #[test]
    fn empty_tree_non_existence() {
        let tree = SnapshotTree::from_sorted(vec![]).unwrap();
        let ProofResponse::Missing { proof, .. } = tree.prove(0, b"anything") else {
            panic!("empty tree must yield missing")
        };
        assert!(proof.empty_tree);
        verify_non_existence(&proof, tree.root()).unwrap();
    }

    #[test]
    fn single_leaf_bounds() {
        let tree = build(&[("m", "x")]);
        let ProofResponse::Missing { proof, .. } = tree.prove(1, b"a") else {
            panic!()
        };
        assert_eq!(proof.bounds.len(), 1);
        verify_non_existence(&proof, tree.root()).unwrap();
    }
}
