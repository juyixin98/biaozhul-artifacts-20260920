//! Block storage basics: content addressing, dedup, refs, integrity checks.

mod common;

use common::{block, both_backends, mem_repo, RepoHarness};

/// Build a small two-level tree: leaf a/b, parent referencing both.
fn build_tree(h: &RepoHarness) -> (String, String, String) {
    let a = block(1, 64);
    let b = block(2, 64);
    let ia = h.repo.add_block(&a, &[]).unwrap();
    let ib = h.repo.add_block(&b, &[]).unwrap();
    let parent_data = format!("children of v1");
    let ip = h
        .repo
        .add_block(parent_data.as_bytes(), &[ia.hash.clone(), ib.hash.clone()])
        .unwrap();
    (ia.hash, ib.hash, ip.hash)
}

#[test]
fn store_read_roundtrip_on_both_backends() {
    both_backends(|h| {
        let data = block(7, 1024);
        let info = h.repo.add_block(&data, &[]).unwrap();
        assert_eq!(info.size, 1024);
        assert!(!info.deduplicated);
        let got = h.repo.get_block(&info.hash).unwrap();
        assert_eq!(got, data);
        let got = h.repo.get_block_verified(&info.hash).unwrap();
        assert_eq!(got, data);
        assert!(h.repo.block_exists(&info.hash));
    });
}

#[test]
fn identical_content_deduplicates() {
    both_backends(|h| {
        let data = block(9, 200);
        let first = h.repo.add_block(&data, &[]).unwrap();
        let second = h.repo.add_block(&data, &[]).unwrap();
        assert_eq!(first.hash, second.hash);
        assert!(second.deduplicated);
        assert_eq!(second.size, data.len() as u64);
        // Total blocks is one — GC sees a single node, and enumeration dedups.
        let report = h.repo.verify().unwrap();
        assert_eq!(report.blocks_total, 1);
        assert_eq!(report.ok, 1);
    });
}

#[test]
fn refs_are_stored_and_traversed() {
    both_backends(|h| {
        let (_a, _b, _p) = build_tree(h);
        // parent must be reachable through refs; verify reports no dangling.
        let r = h.repo.verify().unwrap();
        assert!(r.missing_refs.is_empty(), "unexpected: {:?}", r.missing_refs);
        assert_eq!(r.blocks_total, 3);
        // uploading a block with non-existent refs is rejected
        let ghost1 = "1".repeat(64);
        let ghost2 = "2".repeat(64);
        let missing = h
            .repo
            .add_block(b"no children here", &[ghost1, ghost2])
            .unwrap_err();
        assert!(matches!(
            missing,
            cas_store::StoreError::MissingReference(_)
        ));
    });
}

#[test]
fn dedup_upload_merges_new_refs() {
    both_backends(|h| {
        let leaf = block(3, 32);
        let i_leaf = h.repo.add_block(&leaf, &[]).unwrap();
        let shared = block(4, 32);
        let i1 = h.repo.add_block(&shared, &[]).unwrap();
        // Re-upload identical `shared`, now declaring the leaf as a ref.
        let i2 = h
            .repo
            .add_block(&shared, &[i_leaf.hash.clone()])
            .unwrap();
        assert!(i2.deduplicated);
        assert_eq!(i2.hash, i1.hash);
        assert!(i2.refs.contains(&i_leaf.hash));
        let r = h.repo.verify().unwrap();
        assert!(r.missing_refs.is_empty());
        assert_eq!(r.blocks_total, 2);
    });
}

#[test]
fn named_put_rejects_wrong_hash() {
    both_backends(|h| {
        let data = block(5, 16);
        let wrong = "a".repeat(64);
        let err = h.repo.add_block_named(&wrong, &data, &[]).unwrap_err();
        assert!(matches!(
            err,
            cas_store::StoreError::HashMismatch { .. }
        ));
        // Correct address works.
        let real = cas_store::hash::sha256_hex(&data);
        let info = h.repo.add_block_named(&real, &data, &[]).unwrap();
        assert_eq!(info.hash, real);
    });
}

#[test]
fn malformed_hashes_and_empty_blocks_rejected() {
    both_backends(|h| {
        assert!(matches!(
            h.repo.get_block("nope").unwrap_err(),
            cas_store::StoreError::InvalidRequest(_)
        ));
        assert!(matches!(
            h.repo.add_block(&[], &[]).unwrap_err(),
            cas_store::StoreError::InvalidRequest(_)
        ));
        assert!(matches!(
            h.repo.add_block(b"x", &["zz".repeat(64)]).unwrap_err(),
            cas_store::StoreError::InvalidRequest(_)
        ));
    });
}

#[test]
fn missing_block_is_not_found() {
    both_backends(|h| {
        let h64 = "f".repeat(64);
        assert!(matches!(
            h.repo.get_block(&h64).unwrap_err(),
            cas_store::StoreError::NotFound(_)
        ));
    });
}

#[test]
fn reopen_persists_real_backend_and_repairs_tmp() {
    use cas_store::{RealFs, Repository};
    use std::sync::Arc;
    let temp = common::TempDir::new("reopen");
    let store = temp.path.join("store");
    let info_hash = {
        let repo = Repository::open(&store, Arc::new(RealFs::new())).unwrap();
        let data = block(11, 50);
        let info = repo.add_block(&data, &[]).unwrap();

        // Simulate a crashed write: leave a temp file lying in the shard dir.
        let shard_dir = store.join("blocks").join(&info.hash[0..2]);
        std::fs::write(shard_dir.join(".tmp.999.deadbeef"), b"partial").unwrap();
        info.hash
        // repository dropped here: flock released
    };

    let repo2 = Repository::open(&store, Arc::new(RealFs::new())).unwrap();
    let got = repo2.get_block(&info_hash).unwrap();
    assert_eq!(got.len(), 50);
    let shard_dir = store.join("blocks").join(&info_hash[0..2]);
    let entries: Vec<String> = std::fs::read_dir(&shard_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert!(entries.iter().all(|n| !n.starts_with(".tmp.")));
}

#[test]
fn mem_backend_basic_smoke() {
    let h = mem_repo();
    let info = h.repo.add_block(b"hello", &[]).unwrap();
    assert_eq!(h.repo.get_block(&info.hash).unwrap(), b"hello");
}
