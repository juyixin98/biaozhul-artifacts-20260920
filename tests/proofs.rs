//! Proof generation/verification, tampering resistance and cross-checking
//! against the independent reference verifier.

#[path = "common/independent.rs"]
mod independent;

use independent::{check_absence, check_response, Response as RefResponse};
use merkle_proof_service::hash::empty_root;
use merkle_proof_service::proof::{verify_inclusion, verify_non_existence, ProofResponse};
use merkle_proof_service::tree::{KV, SnapshotTree};

fn kv(pairs: &[(&str, &str)]) -> Vec<KV> {
    pairs
        .iter()
        .map(|(k, v)| KV { key: k.as_bytes().to_vec(), value: v.as_bytes().to_vec() })
        .collect()
}

fn tree(pairs: &[(&str, &str)]) -> SnapshotTree {
    SnapshotTree::from_sorted(kv(pairs)).unwrap()
}

fn as_exists(r: ProofResponse) -> merkle_proof_service::proof::InclusionProof {
    match r {
        ProofResponse::Exists { proof, .. } => proof,
        _ => panic!("expected existence proof"),
    }
}

fn as_missing(r: ProofResponse) -> merkle_proof_service::proof::NonExistenceProof {
    match r {
        ProofResponse::Missing { proof, .. } => proof,
        _ => panic!("expected non-existence proof"),
    }
}

#[test]
fn empty_tree_root_and_absence() {
    let t = SnapshotTree::from_sorted(vec![]).unwrap();
    assert_eq!(t.root(), &empty_root());
    assert_eq!(t.leaf_count(), 0);

    let resp = t.prove(0, b"nope");
    let p = as_missing(resp.clone());
    assert!(p.empty_tree);
    verify_non_existence(&p, t.root()).unwrap();

    // Independent verifier, fed JSON exactly like an external party.
    let value = serde_json::to_value(&resp).unwrap();
    let reference: RefResponse = serde_json::from_value(value).unwrap();
    check_response(&reference, &hex::encode(t.root())).unwrap();
}

#[test]
fn single_leaf_root_is_leaf_hash() {
    let t = tree(&[("only", "data")]);
    let proof = as_exists(t.prove(1, b"only"));
    assert_eq!(proof.path.len(), 0);
    verify_inclusion(&proof, t.root()).unwrap();
    assert_eq!(
        t.root(),
        &merkle_proof_service::hash::hash_leaf(b"only", b"data")
    );
}

#[test]
fn all_leaves_verify_every_size_and_cross_check() {
    for n in 1..=20usize {
        let pairs: Vec<(String, String)> =
            (0..n).map(|i| (format!("key/{i:04}"), format!("value/{i:04}"))).collect();
        let refs: Vec<(&str, &str)> =
            pairs.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
        let t = tree(&refs);

        for (i, (k, v)) in pairs.iter().enumerate() {
            let resp = t.prove(7, k.as_bytes());
            verify_inclusion(&as_exists(resp.clone()), t.root()).unwrap();

            // Tamper-free JSON must also pass the INDEPENDENT verifier.
            let value = serde_json::to_value(&resp).unwrap();
            let reference: RefResponse = serde_json::from_value(value).unwrap();
            check_response(&reference, &hex::encode(t.root())).unwrap();

            if let ProofResponse::Exists { value: val, proof, .. } = &resp {
                assert_eq!(val.0, v.as_bytes());
                assert_eq!(proof.index as usize, i);
            }
        }

        // Missing keys: below, between, above (uses empty value key to also
        // ensure zero-length bytes are ordinary bytes).
        for missing in ["a", "key/0000\x00", "key/0002a", "zzz"] {
            let resp = t.prove(7, missing.as_bytes());
            let p = as_missing(resp.clone());
            assert!(!p.empty_tree);
            verify_non_existence(&p, t.root()).unwrap();
            let value = serde_json::to_value(&resp).unwrap();
            let reference: RefResponse = serde_json::from_value(value).unwrap();
            check_response(&reference, &hex::encode(t.root())).unwrap();
        }
    }
}

#[test]
fn empty_value_is_distinct_from_absence() {
    let t = tree(&[("a", ""), ("b", "x")]);
    // Empty value proves EXISTENCE with value "".
    match t.prove(1, b"a") {
        ProofResponse::Exists { value, proof, .. } => {
            assert!(value.0.is_empty());
            verify_inclusion(&proof, t.root()).unwrap();
        }
        _ => panic!("empty value must be existence"),
    }
    // Different key is a non-existence proof, not an empty value.
    let missing = as_missing(t.prove(1, b"c"));
    assert_eq!(missing.bounds.len(), 1); // right edge ("c" above "b")
    verify_non_existence(&missing, t.root()).unwrap();
}

#[test]
fn non_existence_binds_to_neighbors_and_edges() {
    let t = tree(&[("b", "1"), ("d", "2"), ("f", "3")]);

    // Below minimum: single right bound = leftmost leaf.
    let below = as_missing(t.prove(1, b"a"));
    assert_eq!(below.bounds.len(), 1);
    verify_non_existence(&below, t.root()).unwrap();

    // Above maximum: single left bound = rightmost leaf.
    let above = as_missing(t.prove(1, b"z"));
    assert_eq!(above.bounds.len(), 1);
    verify_non_existence(&above, t.root()).unwrap();

    // Interior: two adjacent bounds.
    let mid = as_missing(t.prove(1, b"c"));
    assert_eq!(mid.bounds.len(), 2);
    verify_non_existence(&mid, t.root()).unwrap();
}

// ---------------------------------------------------------------------------
// Tampering: every mutation must be rejected by BOTH verifiers.
// ---------------------------------------------------------------------------

fn flip_bit(v: &mut serde_json::Value, pointer: &str) {
    let node = v.pointer_mut(pointer).unwrap();
    let s = node.as_str().unwrap().to_string();
    let mut bytes = hex::decode(&s).unwrap();
    bytes[0] ^= 0x01;
    *node = serde_json::Value::String(hex::encode(bytes));
}

#[test]
fn tampered_sibling_hash_rejected() {
    let t = tree(&[("b", "1"), ("d", "2"), ("f", "3"), ("h", "4")]);
    let resp = t.prove(1, b"d");
    let mut v = serde_json::to_value(&resp).unwrap();
    flip_bit(&mut v, "/proof/path/0/sibling_hash");

    let proof = serde_json::from_value(v.clone()).unwrap();
    assert!(verify_inclusion(&as_exists(proof), t.root()).is_err());

    let reference: RefResponse = serde_json::from_value(v).unwrap();
    assert!(check_response(&reference, &hex::encode(t.root())).is_err());
}

#[test]
fn tampered_leaf_key_and_value_rejected() {
    let t = tree(&[("alpha", "1"), ("beta", "2")]);
    let resp = t.prove(1, b"alpha");
    let v = serde_json::to_value(&resp).unwrap();

    // Wrong key in the entry (typical "prove key X but show Y" attack).
    let mut wrong_key = v.clone();
    wrong_key["proof"]["entry"]["key"] = serde_json::json!(hex::encode("alpha2"));
    let proof = serde_json::from_value(wrong_key).unwrap();
    assert!(verify_inclusion(&as_exists(proof), t.root()).is_err());

    // Wrong value.
    let mut wrong_val = v;
    wrong_val["proof"]["entry"]["value"] = serde_json::json!(hex::encode("9"));
    let proof = serde_json::from_value(wrong_val).unwrap();
    assert!(verify_inclusion(&as_exists(proof), t.root()).is_err());
}

#[test]
fn proof_for_other_root_rejected() {
    let t1 = tree(&[("a", "1"), ("b", "2")]);
    let t2 = tree(&[("a", "1"), ("b", "3")]); // different value -> different root
    let p = as_exists(t1.prove(1, b"a"));
    assert!(verify_inclusion(&p, t2.root()).is_err());
}

#[test]
fn flipped_direction_rejected() {
    let t = tree(&[("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")]);
    let mut p = as_exists(t.prove(1, b"c"));
    // "c" is index 2: level-0 side must be "right"; flip it.
    p.path[0].side = merkle_proof_service::proof::Side::Left;
    // The embedded index (2, bit0=0) now contradicts the sides.
    assert!(verify_inclusion(&p, t.root()).is_err());

    // Even if the attacker also rewrites the index to match, the recomputed
    // root cannot match because the pairing order changes.
    let mut p2 = p.clone();
    p2.index = 3;
    assert!(verify_inclusion(&p2, t.root()).is_err());
}

#[test]
fn bogus_index_rejected() {
    let t = tree(&[("a", "1"), ("b", "2"), ("c", "3")]);
    let mut p = as_exists(t.prove(1, b"a"));
    p.index = 1;
    assert!(verify_inclusion(&p, t.root()).is_err());
    p.index = 99;
    assert!(verify_inclusion(&p, t.root()).is_err());
}

#[test]
fn bogus_leaf_count_rejected() {
    // A count that changes the tree depth is caught by path-length check.
    let t = tree(&[("a", "1"), ("b", "2"), ("c", "3")]);
    let mut p = as_exists(t.prove(1, b"a"));
    p.leaf_count = 5; // depth(5)=3 but the path has 2 steps
    assert!(verify_inclusion(&p, t.root()).is_err());

    // Same-depth lie (3 -> 4) is information-theoretically invisible for the
    // leftmost leaf: the extra leaf lives inside a sibling subtree whose hash
    // is carried in the path. It is harmless — the authenticated key/value is
    // still real — so it verifies.
    let mut p = as_exists(t.prove(1, b"a"));
    p.leaf_count = 4;
    assert!(verify_inclusion(&p, t.root()).is_ok());

    // Deflating the count to FAKE a right edge IS caught: in an 8-leaf tree,
    // leaf 6 claimed in a 7-leaf tree would have to be a duplicated last node
    // at level 0, forcing its sibling hash to equal its own hash.
    let pairs: Vec<(String, String)> =
        (0..8).map(|i| (format!("k{i}"), format!("v{i}"))).collect();
    let refs: Vec<(&str, &str)> =
        pairs.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
    let t8 = tree(&refs);
    let mut edge = as_exists(t8.prove(1, b"k6"));
    assert_eq!(edge.index, 6);
    edge.leaf_count = 7;
    assert!(verify_inclusion(&edge, t8.root()).is_err());
}

#[test]
fn absence_attacks_rejected() {
    let t = tree(&[("b", "1"), ("d", "2"), ("f", "3")]);
    let p = as_missing(t.prove(1, b"c"));

    // 1) Move the queried key onto an existing key ("c" -> "d") — must fail.
    let mut v = serde_json::to_value(&p).unwrap();
    v["queried_key"] = serde_json::json!(hex::encode("d"));
    // Rejected by the crate verifier after JSON round-trip.
    let crate_p: merkle_proof_service::proof::NonExistenceProof =
        serde_json::from_value(v.clone()).unwrap();
    assert!(verify_non_existence(&crate_p, t.root()).is_err());
    // And by the independent verifier.
    let parsed: independent::Absence = serde_json::from_value(v).unwrap();
    assert!(check_absence(&parsed, &hex::encode(t.root())).is_err());

    // 2) Drop one interior bound -> only one bound remains and it is not an
    //    edge, so it must be rejected.
    let mut one_bound = p.clone();
    one_bound.bounds.truncate(1);
    assert!(verify_non_existence(&one_bound, t.root()).is_err());

    // 3) Claim empty_tree on a non-empty root.
    let mut lie = p.clone();
    lie.empty_tree = true;
    lie.bounds.clear();
    assert!(verify_non_existence(&lie, t.root()).is_err());

    // 4) Two individually-valid, correctly-ordering bounds that are NOT
    //    adjacent (b at index 0, f at index 2) must still be rejected.
    let mut far = p.clone();
    far.bounds.clear();
    far.bounds.push(p.bounds[0].clone()); // left: b (index 0)
    let f_proof = as_exists(t.prove(1, b"f")); // right: f (index 2)
    far.bounds.push(merkle_proof_service::proof::Bound {
        side: merkle_proof_service::proof::Side::Right,
        proof: f_proof,
    });
    assert!(verify_non_existence(&far, t.root()).is_err());
}

#[test]
fn empty_root_is_a_fixed_constant() {
    // Pinned independently with python -c hashlib.sha256(b'\\x02').
    assert_eq!(
        hex::encode(empty_root()),
        "dbc1b4c900ffe48d575b5da5c638040125f65db0fe3e24494b76ea986457d986"
    );
}
