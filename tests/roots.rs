//! Named roots: atomic publish/switch, validation, listing/deletion.

mod common;

use common::{block, both_backends, fault_repo};
use cas_store::{Fault, FaultOp, RealFs, Repository};
use std::sync::Arc;

#[test]
fn publish_and_switch_root() {
    both_backends(|h| {
        let v1 = h.repo.add_block(b"snapshot v1", &[]).unwrap();
        let v2 = h.repo.add_block(b"snapshot v2", &[]).unwrap();

        let m1 = h.repo.put_root("main", &v1.hash).unwrap();
        assert_eq!(m1.hash, v1.hash);
        assert_eq!(h.repo.get_root("main").unwrap().hash, v1.hash);

        let m2 = h.repo.put_root("main", &v2.hash).unwrap();
        assert!(m2.published_at >= m1.published_at);
        assert_eq!(h.repo.get_root("main").unwrap().hash, v2.hash);
    });
}

#[test]
fn root_must_point_at_existing_block() {
    both_backends(|h| {
        let ghost = "c".repeat(64);
        let err = h.repo.put_root("main", &ghost).unwrap_err();
        assert!(matches!(
            err,
            cas_store::StoreError::MissingReference(_)
        ));
        assert!(matches!(
            h.repo.get_root("main").unwrap_err(),
            cas_store::StoreError::NotFound(_)
        ));
    });
}

#[test]
fn root_name_validation() {
    both_backends(|h| {
        let blk = h.repo.add_block(b"x", &[]).unwrap();
        for bad in ["", ".", "..", ".hidden", "a/b", "a\\b"] {
            assert!(matches!(
                h.repo.put_root(bad, &blk.hash).unwrap_err(),
                cas_store::StoreError::InvalidRequest(_)
            ));
        }
    });
}

#[test]
fn list_and_delete_roots() {
    both_backends(|h| {
        let b1 = h.repo.add_block(block(1, 10).as_slice(), &[]).unwrap();
        let b2 = h.repo.add_block(block(2, 10).as_slice(), &[]).unwrap();
        h.repo.put_root("alpha", &b1.hash).unwrap();
        h.repo.put_root("beta", &b2.hash).unwrap();
        let roots = h.repo.list_roots().unwrap();
        assert_eq!(roots.len(), 2);
        assert_eq!(roots[0].name, "alpha");
        assert_eq!(roots[1].name, "beta");

        h.repo.delete_root("alpha").unwrap();
        assert!(matches!(
            h.repo.delete_root("alpha").unwrap_err(),
            cas_store::StoreError::NotFound(_)
        ));
        let roots = h.repo.list_roots().unwrap();
        assert_eq!(roots.len(), 1);
        assert_eq!(roots[0].name, "beta");
    });
}

#[test]
fn root_publish_crash_leaves_old_root_intact() {
    // Inject a rename fault on the *second* rename of the root manifest:
    // that's the republish of `main` (switch v1 -> v2) crashing at the worst
    // instant.
    let h = fault_repo(vec![Fault {
        path_contains: "/roots/main",
        op: FaultOp::Rename,
        once: Some(2),
    }]);
    let v1 = h.repo.add_block(b"snapshot v1", &[]).unwrap();
    let v2 = h.repo.add_block(b"snapshot v2", &[]).unwrap();

    h.repo.put_root("main", &v1.hash).unwrap();

    // The switch fails exactly at rename — but the OLD root must remain
    // readable and correct (atomic publication boundary).
    let err = h.repo.put_root("main", &v2.hash);
    assert!(err.is_err(), "expected injected rename failure");
    let current = h.repo.get_root("main").unwrap();
    assert_eq!(current.hash, v1.hash, "old root must survive interrupted switch");

    // The repository is still fully usable; a clean retry publishes v2.
    h.repo.put_root("main", &v2.hash).unwrap();
    assert_eq!(h.repo.get_root("main").unwrap().hash, v2.hash);
}

#[test]
fn reopen_after_real_crash_simulation() {
    // On the real FS: first process publishes v1; we manually plant a stale
    // temp manifest in roots/ (crash between write and rename); reopen must
    // show v1 and ignore/clean the temp file.
    let tmp = common::TempDir::new("crash-reopen");
    let vfs = Arc::new(RealFs::new());
    let repo = Repository::open(tmp.path.join("store"), vfs.clone()).unwrap();
    let v1 = repo.add_block(b"v1", &[]).unwrap();
    repo.put_root("main", &v1.hash).unwrap();
    drop(repo);

    let roots = tmp.path.join("store/roots");
    std::fs::write(roots.join(".tmp.1.partial"), b"{\"broken\":").unwrap();

    let repo = Repository::open(tmp.path.join("store"), vfs).unwrap();
    assert_eq!(repo.get_root("main").unwrap().hash, v1.hash);
    let leftover: Vec<_> = std::fs::read_dir(&roots)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with(".tmp."))
        .collect();
    assert!(leftover.is_empty(), "stale temp manifest should be swept: {leftover:?}");
}
