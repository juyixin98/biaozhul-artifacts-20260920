//! Core Merkle + store + independent-verifier integration tests.

use merkle_proof_service::core::{
    build_tree, commit_hash, inner_hash, leaf_hash, prove_index, tree_height,
};
use merkle_proof_service::store::{Op, Store};
use merkle_proof_service::verifier::{verify_proof_slice, Verified};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static SEQ: AtomicU64 = AtomicU64::new(0);

fn temp_db() -> (PathBuf, Store) {
    let mut dir = std::env::temp_dir();
    dir.push(format!(
        "merkle-test-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    let store = Store::open(dir.to_str().unwrap()).expect("open store");
    (dir, store)
}

fn put(key: &str, val: &str) -> (Vec<u8>, Op) {
    (key.as_bytes().to_vec(), Op::Put(val.as_bytes().to_vec()))
}
fn put_bytes(key: &[u8], val: &[u8]) -> (Vec<u8>, Op) {
    (key.to_vec(), Op::Put(val.to_vec()))
}
fn del(key: &str) -> (Vec<u8>, Op) {
    (key.as_bytes().to_vec(), Op::Delete)
}

fn gen(store: &Store, version: Option<u64>, key: &[u8]) -> Value {
    merkle_proof_service::proof::generate(store, version, key).expect("generate proof")
}

fn verify(doc: &Value, root: [u8; 32], key: &[u8]) -> Verified {
    verify_proof_slice(serde_json::to_vec(doc).unwrap().as_slice(), &root, key)
        .expect("verification must succeed")
}

fn root_of(store: &Store, version: Option<u64>) -> [u8; 32] {
    store.root_info(version).expect("root").root
}

// ---------------------------------------------------------------------------

#[test]
fn empty_tree_root_is_zero_commit_and_absence_verifies() {
    let (_d, store) = temp_db();
    // No commit published yet → queries report 404 rather than a fake root.
    assert!(store.root_info(None).is_err());

    // First batch that net-deletes nothing creates version 1 whose *state* is
    // empty: this pins the empty-tree commit historically.
    let out = store
        .commit(vec![put("ghost", ""), del("ghost")])
        .expect("empty-state commit");
    assert_eq!(out.leaf_count, 0);
    let root = out.root;
    assert_ne!(root, [0u8; 32], "empty commit is a tagged hash, not zero");

    // Commit hash must bind version 1 + zero top + n=0 + height=0.
    assert_eq!(root, commit_hash(1, &[0u8; 32], 0, 0));

    let doc = gen(&store, Some(1), b"anything");
    assert_eq!(doc["kind"], "non_existence");
    assert_eq!(doc["leaf_count"], 0);
    assert!(doc["prev"].is_null());
    assert!(doc["next"].is_null());
    match verify(&doc, root, b"anything") {
        Verified::Absent(b) => {
            assert!(b.prev.is_none() && b.next.is_none());
        }
        other => panic!("expected absent, got {other:?}"),
    }
}

#[test]
fn single_leaf_existence_path_is_empty() {
    let (_d, store) = temp_db();
    store.commit(vec![put("only", "solo")]).unwrap();
    let root = root_of(&store, Some(1));

    let doc = gen(&store, Some(1), b"only");
    assert_eq!(doc["kind"], "existence");
    assert_eq!(doc["height"], 0);
    assert!(doc["path"].as_array().unwrap().is_empty());
    match verify(&doc, root, b"only") {
        Verified::Present(v) => assert_eq!(v, b"solo"),
        other => panic!("{other:?}"),
    }

    // Key before the only leaf → boundary absence with next@0.
    let before = gen(&store, Some(1), b"aaaa");
    assert!(before["prev"].is_null());
    assert_eq!(before["next"]["index"], 0);
    assert!(matches!(
        verify(&before, root, b"aaaa"),
        Verified::Absent(_)
    ));

    // Key after → boundary absence with prev@last(=0).
    let after = gen(&store, Some(1), b"zzzz");
    assert_eq!(after["prev"]["index"], 0);
    assert!(after["next"].is_null());
    assert!(matches!(verify(&after, root, b"zzzz"), Verified::Absent(_)));
}

#[test]
fn duplicate_keys_last_write_wins_in_input_order() {
    let (_d, store) = temp_db();
    let out = store
        .commit(vec![
            put("k", "first"),
            put("a", "1"),
            put("k", "second"),
            put("k", "third"),
            put("k", "fourth"),
        ])
        .unwrap();
    assert_eq!(out.leaf_count, 2);
    let root = out.root;
    let doc = gen(&store, Some(1), b"k");
    match verify(&doc, root, b"k") {
        Verified::Present(v) => assert_eq!(v, b"fourth"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn empty_value_is_present_and_distinct_from_delete() {
    let (_d, store) = temp_db();
    // Put empty value; existence proof with value_hex == "".
    store.commit(vec![put("e", "")]).unwrap();
    let root1 = root_of(&store, Some(1));
    let doc = gen(&store, Some(1), b"e");
    assert_eq!(doc["kind"], "existence");
    assert_eq!(doc["value_hex"], "");
    assert!(matches!(
        verify(&doc, root1, b"e"),
        Verified::Present(ref v) if v.is_empty()
    ));

    // Delete the key: same key now gets a non-existence proof and a new root.
    store.commit(vec![del("e")]).unwrap();
    let root2 = root_of(&store, Some(2));
    assert_ne!(root1, root2);
    let gone = gen(&store, Some(2), b"e");
    assert_eq!(gone["kind"], "non_existence");
    assert!(matches!(verify(&gone, root2, b"e"), Verified::Absent(_)));

    // Cross-version binding: v2 proof must not verify under v1 root and vice
    // versa — the commit hash includes the version number.
    assert!(
        verify_proof_slice(serde_json::to_vec(&gone).unwrap().as_slice(), &root1, b"e").is_err()
    );
}

#[test]
fn historical_roots_remain_queryable_and_immutable() {
    let (_d, store) = temp_db();
    let r1 = store.commit(vec![put("a", "1")]).unwrap().root;
    let r2 = store
        .commit(vec![put("b", "2"), put("c", "3")])
        .unwrap()
        .root;
    let r3 = store.commit(vec![del("a")]).unwrap().root;
    let roots = store.list_roots().unwrap();
    assert_eq!(
        roots.iter().map(|r| r.version).collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    assert_eq!((roots[0].root, roots[1].root, roots[2].root), (r1, r2, r3));

    // v1 snapshot still proves a=1 even though current state deleted it.
    let doc = gen(&store, Some(1), b"a");
    assert!(matches!(verify(&doc, r1, b"a"), Verified::Present(v) if v == b"1"));
    // Under v1, b did not exist.
    let absent_then = gen(&store, Some(1), b"b");
    assert!(matches!(
        verify(&absent_then, r1, b"b"),
        Verified::Absent(_)
    ));
    // Unknown versions 404.
    assert!(store.root_info(Some(42)).is_err());
    assert!(store.root_info(Some(0)).is_err());
}

#[test]
fn odd_promoted_paths_replay_at_every_tree_size() {
    // Sizes 1..=8 cover every promotion shape (3,5,6,7 leaves all promote on
    // at least one level). Every leaf's proof must verify and rebuild to root.
    for n in 1u64..=8 {
        let (_d, store) = temp_db();
        let mut writes = Vec::new();
        for i in 0..n {
            writes.push(put(&format!("k{i:02}"), &format!("v{i}")));
        }
        let out = store.commit(writes).unwrap();
        let root = out.root;
        for i in 0..n {
            let key = format!("k{i:02}");
            let doc = gen(&store, Some(1), key.as_bytes());
            match verify(&doc, root, key.as_bytes()) {
                Verified::Present(v) => assert_eq!(v, format!("v{i}").as_bytes()),
                other => panic!("n={n} i={i}: {other:?}"),
            }
        }
        // Between every adjacent pair: absence proof with consecutive indices.
        for i in 0..n.saturating_sub(1) {
            let mid = format!("k{i:02}M");
            let doc = gen(&store, Some(1), mid.as_bytes());
            assert_eq!(doc["prev"]["index"], i);
            assert_eq!(doc["next"]["index"], i + 1);
            assert!(matches!(
                verify(&doc, root, mid.as_bytes()),
                Verified::Absent(_)
            ));
        }
    }
}

#[test]
fn tampered_sibling_hash_is_rejected() {
    let (_d, store) = temp_db();
    store
        .commit(
            (0..5)
                .map(|i| put(&format!("k{i}"), &format!("v{i}")))
                .collect(),
        )
        .unwrap();
    let root = root_of(&store, None);
    let mut doc = gen(&store, None, b"k2");

    // Corrupt one sibling hash.
    let path = doc["path"].as_array_mut().unwrap();
    let step = path.iter_mut().find(|s| s["hash"].is_string()).unwrap();
    let mut bad = hex::decode(step["hash"].as_str().unwrap()).unwrap();
    bad[0] ^= 0x01;
    step["hash"] = Value::String(hex::encode(bad));

    let err = verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &root, b"k2")
        .expect_err("must reject tampered sibling");
    assert!(
        err.to_string().contains("root") || err.to_string().contains("top"),
        "unexpected reason: {err}"
    );
}

#[test]
fn wrong_trusted_root_is_rejected() {
    let (_d, store) = temp_db();
    store
        .commit(vec![put("a", "1"), put("b", "2"), put("c", "3")])
        .unwrap();
    let doc = gen(&store, None, b"b");
    let mut wrong = root_of(&store, None);
    wrong[0] ^= 0xff;
    let err = verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &wrong, b"b")
        .expect_err("must reject wrong root");
    assert!(err.to_string().contains("trusted root"));
}

#[test]
fn wrong_query_key_breaks_existence_and_absence() {
    let (_d, store) = temp_db();
    store
        .commit(vec![put("alpha", "1"), put("beta", "2")])
        .unwrap();
    let root = root_of(&store, None);

    // Existence proof for alpha verified against key beta → rejected.
    let doc = gen(&store, None, b"alpha");
    assert!(
        verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &root, b"beta").is_err()
    );

    // Non-existence proof for key between alpha/beta verified for alpha →
    // rejected: prev-key inequality fails (alpha < alpha is false).
    let absent = gen(&store, None, b"alpha0");
    assert!(verify_proof_slice(
        serde_json::to_vec(&absent).unwrap().as_slice(),
        &root,
        b"alpha"
    )
    .is_err());
}

#[test]
fn existence_proof_cannot_be_substituted_as_absence() {
    let (_d, store) = temp_db();
    store
        .commit(vec![put("a", "1"), put("b", "2"), put("c", "3")])
        .unwrap();
    let root = root_of(&store, None);

    // Hand-craft a malicious absence proof for existing key "b", using the
    // HONEST branches for a(index0) and c(index2): non-adjacent → rejected.
    let ba = gen(&store, None, b"a");
    let bc = gen(&store, None, b"c");
    let fake = serde_json::json!({
        "kind": "non_existence",
        "version": ba["version"], "root": ba["root"], "top": ba["top"],
        "leaf_count": ba["leaf_count"], "height": ba["height"],
        "query_key_hex": hex::encode(b"b"),
        "prev": {
            "index": ba["index"], "key_hex": ba["key_hex"],
            "value_hex": ba["value_hex"], "path": ba["path"]
        },
        "next": {
            "index": bc["index"], "key_hex": bc["key_hex"],
            "value_hex": bc["value_hex"], "path": bc["path"]
        }
    });
    let err = verify_proof_slice(serde_json::to_vec(&fake).unwrap().as_slice(), &root, b"b")
        .expect_err("non-adjacent neighbours must be rejected");
    assert!(err.to_string().contains("non-adjacent"));
}

#[test]
fn tampered_leaf_value_is_rejected() {
    let (_d, store) = temp_db();
    store.commit(vec![put("k", "v")]).unwrap();
    let root = root_of(&store, None);
    let mut doc = gen(&store, None, b"k");
    doc["value_hex"] = Value::String(hex::encode("x"));
    // Rebuilding the leaf hash with a different value fails the root check.
    assert!(verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &root, b"k").is_err());
}

#[test]
fn forged_promotion_step_is_rejected() {
    // 2 leaves: index 0 has a real right sibling. Forge its first step as
    // `promoted`; the replay position rules must reject it.
    let (_d, store) = temp_db();
    store.commit(vec![put("a", "1"), put("b", "2")]).unwrap();
    let root = root_of(&store, None);
    let mut doc = gen(&store, None, b"a");
    doc["path"][0] = serde_json::json!({"side": "promoted", "hash": null});
    assert!(verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &root, b"a").is_err());
}

#[test]
fn raw_binary_keys_and_values_round_trip() {
    let (_d, store) = temp_db();
    let key = vec![0x00, 0xff, 0x10, 0x7f];
    let val = vec![];
    let val2 = vec![0x01, 0x02, 0x00, 0x03];
    store
        .commit(vec![put_bytes(&key, &val), put_bytes(&[0xaa], &val2)])
        .unwrap();
    let root = root_of(&store, None);
    let doc = gen(&store, None, &key);
    match verify(&doc, root, &key) {
        Verified::Present(v) => assert!(v.is_empty()),
        other => panic!("{other:?}"),
    }
    // Byte order: 0x00.. sorts before 0xaa.
    let mid = vec![0x55];
    let absent = gen(&store, None, &mid);
    assert_eq!(absent["prev"]["key_hex"], hex::encode(&key));
    assert_eq!(absent["next"]["key_hex"], hex::encode([0xaa]));
    assert!(matches!(verify(&absent, root, &mid), Verified::Absent(_)));
}

#[test]
fn restart_rebuilds_state_and_roots_from_manifest() {
    let mut dir = std::env::temp_dir();
    dir.push(format!(
        "merkle-restart-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);

    let roots: Vec<[u8; 32]> = {
        let store = Store::open(dir.to_str().unwrap()).unwrap();
        let r1 = store
            .commit(vec![put("a", "1"), put("b", "2")])
            .unwrap()
            .root;
        let r2 = store.commit(vec![put("c", "3"), del("a")]).unwrap().root;
        // Reopen WITH the same directory while first handle is dropped below.
        drop(store);
        vec![r1, r2]
    };

    // Simulate a process restart: fresh Store over the same RocksDB directory.
    let store = Store::open(dir.to_str().unwrap()).expect("reopen");
    assert_eq!(store.current_version(), 2);
    assert_eq!(store.root_info(Some(1)).unwrap().root, roots[0]);
    assert_eq!(store.root_info(Some(2)).unwrap().root, roots[1]);

    // A was deleted after v1: current tree has b,c only.
    let cur = store.current_snapshot();
    assert!(!cur.contains_key(&b"a"[..]));
    assert_eq!(cur.get(&b"b"[..]).unwrap(), b"2");
    assert_eq!(cur.get(&b"c"[..]).unwrap(), b"3");

    // New commit continues at version 3 and builds on the recovered state.
    let out = store.commit(vec![put("d", "4")]).unwrap();
    assert_eq!(out.version, 3);
    let doc = gen(&store, Some(3), b"a");
    assert_eq!(doc["kind"], "non_existence");
    assert!(matches!(verify(&doc, out.root, b"a"), Verified::Absent(_)));
    // History intact post-restart.
    assert_eq!(store.list_roots().unwrap().len(), 3);
}

#[test]
fn failed_publish_does_not_expose_half_root() {
    // If a write batch errors (here: empty batch / empty key are rejected
    // before any db write), version pointer must not advance.
    let (_d, store) = temp_db();
    store.commit(vec![put("a", "1")]).unwrap();
    assert!(store.commit(vec![]).is_err());
    assert!(store
        .commit(vec![(vec![], Op::Put(b"x".to_vec()))])
        .is_err());
    assert_eq!(store.current_version(), 1);
    assert_eq!(store.list_roots().unwrap().len(), 1);
}

#[test]
fn core_domain_separation_and_sorting_unit_rules() {
    assert_eq!(tree_height(0), 0);
    assert_eq!(tree_height(1), 0);
    assert_eq!(tree_height(2), 1);
    assert_eq!(tree_height(3), 2);
    assert_eq!(tree_height(8), 3);
    assert_eq!(tree_height(9), 4);

    // Inner preimage of (A,B) differs from (B,A): order matters.
    let a = leaf_hash(b"a", b"1");
    let b = leaf_hash(b"b", b"1");
    assert_ne!(inner_hash(&a, &b), inner_hash(&b, &a));

    // Hand-verify the spec's exact leaf bytes.
    use sha2::{Digest, Sha256};
    let mut expect = vec![0x00u8];
    expect.extend_from_slice(&1u32.to_be_bytes());
    expect.push(b'a');
    expect.extend_from_slice(&1u32.to_be_bytes());
    expect.push(b'1');
    let expect_hash: [u8; 32] = Sha256::digest(&expect).into();
    assert_eq!(leaf_hash(b"a", b"1"), expect_hash);

    // prove_index for odd-count tree promotes the last node (3 leaves):
    // index 2 is promoted at level 0, then paired as a right child at level 1.
    let t = build_tree(vec![
        (b"a".to_vec(), b"1".to_vec()),
        (b"b".to_vec(), b"2".to_vec()),
        (b"c".to_vec(), b"3".to_vec()),
    ]);
    use merkle_proof_service::core::PathEntry;
    match prove_index(&t, 2).as_slice() {
        [PathEntry::Promoted, PathEntry::Left(_)] => {}
        other => panic!("unexpected path: {other:?}"),
    }
}
