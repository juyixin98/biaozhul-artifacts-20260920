//! RocksDB persistence tests: batch semantics, empty value vs delete,
//! immutable history roots, and reopen (restart) recovery.

use merkle_proof_service::hash::{empty_root, hash_branch, hash_leaf};
use merkle_proof_service::proof::ProofResponse;
use merkle_proof_service::{Store, WriteOp};
use tempfile::TempDir;

fn put(k: &str, v: &str) -> WriteOp {
    WriteOp::Put { key: k.as_bytes().to_vec(), value: v.as_bytes().to_vec() }
}
fn del(k: &str) -> WriteOp {
    WriteOp::Delete { key: k.as_bytes().to_vec() }
}

#[test]
fn fresh_db_starts_at_version_zero_empty_tree() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();
    assert_eq!(store.current_version().unwrap(), 0);
    let v0 = store.version_info(0).unwrap();
    assert!(v0.is_none()); // no stored manifest for the implicit v0
    let tree = store.tree_at(0).unwrap();
    assert_eq!(tree.root(), &empty_root());
    assert_eq!(tree.leaf_count(), 0);
    assert!(store.version_info(7).unwrap().is_none());
    assert!(store.tree_at(1).is_err());
}

#[test]
fn batch_publishes_immutable_roots_and_history_is_queryable() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();

    let v1 = store
        .apply_batch(vec![put("a", "1"), put("b", "2"), put("c", "3")])
        .unwrap();
    assert_eq!(v1.version, 1);
    assert_eq!(v1.leaf_count, 3);

    // v1 root stays fixed after further batches.
    let root_v1 = v1.root;
    let v2 = store
        .apply_batch(vec![put("a", "1-updated"), del("nonexistent")])
        .unwrap();
    assert_eq!(v2.version, 2);
    assert_ne!(root_v1, v2.root);

    let v3 = store.apply_batch(vec![del("b")]).unwrap();
    assert_eq!(v3.version, 3);
    assert_eq!(v3.leaf_count, 2);

    assert_eq!(store.current_version().unwrap(), 3);

    // Historical manifests are intact.
    assert_eq!(store.version_info(1).unwrap().unwrap().root, root_v1);
    assert_eq!(store.version_info(2).unwrap().unwrap().root, v2.root);

    // Historical trees prove HISTORICAL values.
    let t1 = store.tree_at(1).unwrap();
    assert_eq!(t1.leaf_count(), 3);
    match t1.prove(1, b"a") {
        ProofResponse::Exists { value, .. } => assert_eq!(value.0, b"1"),
        _ => panic!(),
    }
    // At v2, "a" changed.
    let t2 = store.tree_at(2).unwrap();
    match t2.prove(2, b"a") {
        ProofResponse::Exists { value, .. } => assert_eq!(value.0, b"1-updated"),
        _ => panic!(),
    }
    // At v3, "b" is gone (non-existence), but existed at v1 and v2.
    let t3 = store.tree_at(3).unwrap();
    assert!(matches!(t3.prove(3, b"b"), ProofResponse::Missing { .. }));
    let t2b = store.tree_at(2).unwrap();
    assert!(matches!(t2b.prove(2, b"b"), ProofResponse::Exists { .. }));

    // All three published versions list.
    let versions = store.list_versions().unwrap();
    assert_eq!(versions.iter().map(|v| v.version).collect::<Vec<_>>(), vec![1, 2, 3]);
}

#[test]
fn last_write_in_batch_wins() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();

    let info = store
        .apply_batch(vec![
            put("k", "first"),
            put("k", "second"),
            put("k", ""),       // last put: empty value wins
            put("k", "fourth"), // then this is the actual last op
        ])
        .unwrap();
    assert_eq!(info.leaf_count, 1);
    assert_eq!(store.get_current(b"k").unwrap(), Some(b"fourth".to_vec()));

    // Last op delete wins over earlier puts.
    store
        .apply_batch(vec![put("k", "x"), put("m", "y"), del("k")])
        .unwrap();
    assert_eq!(store.get_current(b"k").unwrap(), None);
    assert_eq!(store.get_current(b"m").unwrap(), Some(b"y".to_vec()));

    // Put after delete in the same batch resurrects the key.
    let res = store
        .apply_batch(vec![del("m"), put("m", "resurrected")])
        .unwrap();
    assert_eq!(res.leaf_count, 1);
    assert_eq!(store.get_current(b"m").unwrap(), Some(b"resurrected".to_vec()));
}

#[test]
fn empty_value_distinct_from_delete() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();

    store.apply_batch(vec![put("empty", ""), put("full", "x")]).unwrap();

    // Explicit empty value: exists, returns empty bytes.
    assert_eq!(store.get_current(b"empty").unwrap(), Some(vec![]));
    // Never written / deleted: absent.
    assert_eq!(store.get_current(b"ghost").unwrap(), None);

    let info = store.apply_batch(vec![del("empty")]).unwrap();
    assert_eq!(store.get_current(b"empty").unwrap(), None);

    // Root for {empty:"", full:"x"} differs from root after deleting empty.
    let before = store.version_info(1).unwrap().unwrap().root;
    assert_ne!(before, info.root);

    // Sanity: the v1 root commits the empty-valued leaf distinctly from a
    // delete (it is the branch over the empty-value leaf and "full").
    assert_eq!(
        before,
        hash_branch(&hash_leaf(b"empty", b""), &hash_leaf(b"full", b"x"))
    );
}

#[test]
fn empty_batch_is_a_no_op_version() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let v1 = store.apply_batch(vec![]).unwrap();
    assert_eq!(v1.leaf_count, 0);
    assert_eq!(v1.root, empty_root());
    // Still published and queryable.
    assert_eq!(store.tree_at(1).unwrap().root(), &empty_root());
}

#[test]
fn reopens_and_recovers_everything_after_restart() {
    let dir = TempDir::new().unwrap();
    let roots;
    let final_state;
    {
        let store = Store::open(dir.path()).unwrap();
        let v1 = store
            .apply_batch(vec![put("alpha", "1"), put("beta", "2"), put("gamma", "3")])
            .unwrap();
        let v2 = store
            .apply_batch(vec![put("beta", "two"), put("delta", ""), del("alpha")])
            .unwrap();
        roots = (v1.root, v2.root);
        final_state = (
            store.get_current(b"alpha").unwrap(),
            store.get_current(b"beta").unwrap(),
            store.get_current(b"gamma").unwrap(),
            store.get_current(b"delta").unwrap(),
        );
        drop(store);
    }

    // Simulate service restart on the same directory.
    let store = Store::open(dir.path()).unwrap();
    assert_eq!(store.current_version().unwrap(), 2);
    assert_eq!(store.version_info(1).unwrap().unwrap().root, roots.0);
    assert_eq!(store.version_info(2).unwrap().unwrap().root, roots.1);

    assert_eq!(store.get_current(b"alpha").unwrap(), final_state.0);
    assert_eq!(store.get_current(b"beta").unwrap(), final_state.1);
    assert_eq!(store.get_current(b"gamma").unwrap(), final_state.2);
    assert_eq!(store.get_current(b"delta").unwrap(), final_state.3);

    // alpha absent after restart → non-existence proof under v2.
    let t2 = store.tree_at(2).unwrap();
    assert!(matches!(t2.prove(2, b"alpha"), ProofResponse::Missing { .. }));
    // And history still proves alpha existed at v1.
    let t1 = store.tree_at(1).unwrap();
    assert!(matches!(t1.prove(1, b"alpha"), ProofResponse::Exists { .. }));

    // New batches continue numbering after recovery.
    let v3 = store.apply_batch(vec![put("epsilon", "5")]).unwrap();
    assert_eq!(v3.version, 3);
}

#[test]
fn unknown_version_returns_distinct_error() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();
    let err = store.tree_at(42).unwrap_err();
    assert!(matches!(err, merkle_proof_service::Error::UnknownVersion(42)));
}
