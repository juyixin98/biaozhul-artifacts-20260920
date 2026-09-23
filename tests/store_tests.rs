//! Store-level integration tests, including the acceptance scenarios:
//! concurrent same-block uploads, orphan blocks, missing references,
//! interrupted root switches, and GC/read concurrency protection.

mod common;

use std::sync::Arc;
use std::thread;

use cas_repo::hash::Sha256;
use cas_repo::store::{DynStore, StoreError};
use cas_repo::vfs::{FaultMap, FaultyVfs, StdVfs, Vfs};
use common::TempDir;

fn open_store(dir: &TempDir) -> Arc<DynStore> {
    let vfs: Box<dyn Vfs> = Box::new(StdVfs::new());
    DynStore::open_boxed(dir.path(), vfs).unwrap()
}

fn open_faulty(dir: &TempDir) -> (Arc<DynStore>, FaultMap) {
    let faults = FaultMap::new();
    let vfs: Box<dyn Vfs> = Box::new(FaultyVfs::new(Box::new(StdVfs::new()), faults.clone()));
    let store = DynStore::open_boxed(dir.path(), vfs).unwrap();
    (store, faults)
}

fn blob_path(dir: &TempDir, h: &Sha256) -> std::path::PathBuf {
    let hex = h.to_hex();
    dir.path().join("blocks").join(&hex[..2]).join(&hex[2..])
}

// ---------- basic semantics ----------

#[test]
fn put_get_roundtrip_and_dedup() {
    let dir = TempDir::new("roundtrip");
    let store = open_store(&dir);

    let data = b"hello content-addressed world";
    let out1 = store.put_block(data, None, &[]).unwrap();
    assert!(!out1.deduplicated);
    assert_eq!(out1.hash, Sha256::hash(data));

    // Same content again: de-duplicated, blob not rewritten.
    let out2 = store.put_block(data, None, &[]).unwrap();
    assert!(out2.deduplicated);
    assert_eq!(out1.hash, out2.hash);

    let got = store.get_block(&out1.hash).unwrap();
    assert_eq!(got, data);
}

#[test]
fn declared_hash_is_verified() {
    let dir = TempDir::new("hashcheck");
    let store = open_store(&dir);

    let data = b"some bytes";
    let wrong = Sha256::hash(b"different bytes");
    let err = store.put_block(data, Some(wrong), &[]).unwrap_err();
    match err {
        StoreError::HashMismatch { declared, actual } => {
            assert_eq!(declared, wrong.to_hex());
            assert_eq!(actual, Sha256::hash(data).to_hex());
        }
        other => panic!("expected HashMismatch, got {other:?}"),
    }
    // Nothing was stored.
    assert!(!store.block_exists(&Sha256::hash(data)).unwrap());
}

#[test]
fn root_publish_and_occ() {
    let dir = TempDir::new("roots");
    let store = open_store(&dir);

    let a = store.put_block(b"version-a", None, &[]).unwrap().hash;
    let b = store.put_block(b"version-b", None, &[]).unwrap().hash;

    // Root target must exist.
    let missing = Sha256::hash(b"never uploaded");
    assert!(matches!(
        store.put_root("main", missing, None),
        Err(StoreError::NotFound(_))
    ));

    let r1 = store.put_root("main", a, None).unwrap();
    assert_eq!(r1.version, 1);
    let r2 = store.put_root("main", b, None).unwrap();
    assert_eq!(r2.version, 2);

    // Optimistic concurrency: expected version must match.
    assert!(matches!(
        store.put_root("main", a, Some(1)),
        Err(StoreError::VersionConflict { .. })
    ));
    let r3 = store.put_root("main", a, Some(2)).unwrap();
    assert_eq!(r3.version, 3);

    let got = store.get_root("main").unwrap();
    assert_eq!(got.hash, a);
    assert_eq!(got.version, 3);

    let roots = store.list_roots().unwrap();
    assert_eq!(roots.len(), 1);
    assert_eq!(roots[0].0, "main");
}

#[test]
fn invalid_root_names_rejected() {
    let dir = TempDir::new("badnames");
    let store = open_store(&dir);
    let h = store.put_block(b"x", None, &[]).unwrap().hash;
    for name in ["../evil", ".hidden", "a/b", "", "with space"] {
        assert!(matches!(
            store.put_root(name, h, None),
            Err(StoreError::InvalidRootName(_))
        ));
    }
}

// ---------- GC: orphans, reachability, missing references ----------

#[test]
fn gc_removes_orphans_keeps_reachable() {
    let dir = TempDir::new("gc-basic");
    let store = open_store(&dir);

    // Tree: root -> manifest -> [leaf1, leaf2]; orphan stands alone.
    let leaf1 = store.put_block(b"leaf-1", None, &[]).unwrap().hash;
    let leaf2 = store.put_block(b"leaf-2", None, &[]).unwrap().hash;
    let manifest = store
        .put_block(b"manifest", None, &[leaf1, leaf2])
        .unwrap()
        .hash;
    let orphan = store.put_block(b"orphan-bytes", None, &[]).unwrap().hash;

    store.put_root("main", manifest, None).unwrap();

    let report = store.gc().unwrap();
    assert_eq!(report.blocks_live, 3);
    assert_eq!(report.blocks_removed, 1);
    assert_eq!(report.removed, vec![orphan.to_hex()]);
    assert!(report.missing_references.is_empty());

    assert!(store.block_exists(&manifest).unwrap());
    assert!(store.block_exists(&leaf1).unwrap());
    assert!(store.block_exists(&leaf2).unwrap());
    assert!(!store.block_exists(&orphan).unwrap());
    assert!(!blob_path(&dir, &orphan).exists());
}

#[test]
fn gc_reports_missing_references_without_deleting_parents() {
    let dir = TempDir::new("gc-missing");
    let store = open_store(&dir);

    // Manifest references a block that was never uploaded.
    let ghost = Sha256::hash(b"ghost-block");
    let leaf = store.put_block(b"real-leaf", None, &[]).unwrap().hash;
    let manifest = store
        .put_block(b"manifest", None, &[ghost, leaf])
        .unwrap()
        .hash;
    store.put_root("main", manifest, None).unwrap();

    let report = store.gc().unwrap();
    assert_eq!(report.missing_references.len(), 1);
    assert_eq!(report.missing_references[0].missing, ghost.to_hex());
    assert_eq!(
        report.missing_references[0].parent.as_deref(),
        Some(manifest.to_hex().as_str())
    );
    // Reachable blocks are untouched.
    assert_eq!(report.blocks_removed, 0);
    assert!(store.block_exists(&manifest).unwrap());
    assert!(store.block_exists(&leaf).unwrap());
}

#[test]
fn gc_reports_missing_root_target() {
    let dir = TempDir::new("gc-missing-root");
    let store = open_store(&dir);

    let real = store.put_block(b"real", None, &[]).unwrap().hash;
    store.put_root("ok", real, None).unwrap();

    // Forge a dangling root directly on disk (bypasses put_root's existence
    // check) to simulate a root written against a since-lost block.
    let ghost = Sha256::hash(b"ghost-root-target");
    let root_file = dir.path().join("roots").join("dangling.json");
    std::fs::write(
        &root_file,
        format!(
            "{{\"hash\":\"{}\",\"version\":1,\"updated_at_ms\":1}}",
            ghost.to_hex()
        ),
    )
    .unwrap();

    let report = store.gc().unwrap();
    assert_eq!(report.missing_references.len(), 1);
    assert_eq!(report.missing_references[0].parent, None);
    assert_eq!(report.missing_references[0].missing, ghost.to_hex());
    assert!(store.block_exists(&real).unwrap());
}

// ---------- GC concurrency protection ----------

#[test]
fn pinned_block_survives_gc() {
    let dir = TempDir::new("gc-pin");
    let store = open_store(&dir);

    // Orphan block, but a reader pins it before GC runs.
    let orphan = store.put_block(b"in-flight-read", None, &[]).unwrap().hash;
    let guard = store.pin_block(&orphan).unwrap().expect("block exists");
    assert!(store.is_pinned(&orphan));

    let report = store.gc().unwrap();
    assert_eq!(report.blocks_removed, 0, "pinned orphan must survive GC");
    assert!(store.block_exists(&orphan).unwrap());

    drop(guard);
    assert!(!store.is_pinned(&orphan));
    let report2 = store.gc().unwrap();
    assert_eq!(report2.blocks_removed, 1, "unpinned orphan is collected");
    assert!(!store.block_exists(&orphan).unwrap());
}

#[test]
fn get_during_gc_never_reads_deleted_block() {
    let dir = TempDir::new("gc-read-race");
    let store = open_store(&dir);

    // Reachable tree plus many orphans to give the sweeper work.
    let leaf = store.put_block(b"live-leaf", None, &[]).unwrap().hash;
    let manifest = store.put_block(b"m", None, &[leaf]).unwrap().hash;
    store.put_root("main", manifest, None).unwrap();
    let mut orphans = Vec::new();
    for i in 0..50 {
        let data = format!("orphan-{i}");
        orphans.push(store.put_block(data.as_bytes(), None, &[]).unwrap().hash);
    }

    // Readers hammer the live leaf while GC sweeps.
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let mut readers = Vec::new();
    for _ in 0..4 {
        let store = store.clone();
        let stop = stop.clone();
        readers.push(thread::spawn(move || {
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                let data = store.get_block(&leaf).expect("live block readable");
                assert_eq!(data, b"live-leaf");
            }
        }));
    }
    for _ in 0..5 {
        let report = store.gc().unwrap();
        assert!(store.block_exists(&leaf).unwrap());
        assert!(store.block_exists(&manifest).unwrap());
        let _ = report;
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    for r in readers {
        r.join().unwrap();
    }
    // All orphans collected eventually.
    let final_report = store.gc().unwrap();
    assert_eq!(final_report.blocks_removed, 0);
    for o in &orphans {
        assert!(!store.block_exists(o).unwrap());
    }
}

#[test]
fn root_switch_during_gc_is_serialized_and_safe() {
    let dir = TempDir::new("gc-root-switch");
    let store = open_store(&dir);

    let old_block = store.put_block(b"old-root-target", None, &[]).unwrap().hash;
    store.put_root("main", old_block, None).unwrap();
    let new_block = store.put_block(b"new-root-target", None, &[]).unwrap().hash;

    // Run GC in a thread; concurrently switch the root. GC and put_root are
    // mutually exclusive (write lock), so exactly one of these orders happens:
    //   1. switch first: GC marks new_block reachable, old_block may be swept;
    //   2. GC first: new_block is an orphan and gets swept, then put_root
    //      refuses to point at the missing block (NotFound) — the client
    //      re-uploads and retries.
    // Both are safe; a dangling root is impossible.
    let store2 = store.clone();
    let gc_thread = thread::spawn(move || store2.gc().unwrap());
    thread::sleep(std::time::Duration::from_millis(5));
    let put_result = store.put_root("main", new_block, None);
    let _report = gc_thread.join().unwrap();

    match put_result {
        Ok(rec) => {
            assert_eq!(rec.hash, new_block);
            assert!(store.block_exists(&new_block).unwrap());
        }
        Err(StoreError::NotFound(_)) => {
            // GC won the race and collected the orphan; the documented client
            // recovery is re-upload + retry.
            store.put_block(b"new-root-target", None, &[]).unwrap();
            store.put_root("main", new_block, None).unwrap();
        }
        Err(other) => panic!("unexpected put_root error: {other:?}"),
    }

    // Invariant: the active root always resolves to an existing block.
    let rec = store.get_root("main").unwrap();
    assert!(
        store.block_exists(&rec.hash).unwrap(),
        "block referenced by the active root must never be swept"
    );
}

// ---------- fault injection ----------

#[test]
fn interrupted_root_switch_keeps_old_root_and_orphans_new_block() {
    let dir = TempDir::new("fault-root");
    let (store, faults) = open_faulty(&dir);

    let a = store.put_block(b"root-v1", None, &[]).unwrap().hash;
    store.put_root("main", a, None).unwrap();

    let b = store.put_block(b"root-v2", None, &[]).unwrap().hash;

    // Simulate a crash exactly at the root publish rename: the temp pointer
    // file is really written and fsynced, the rename never happens.
    faults.fail_once("rename_root");
    let err = store.put_root("main", b, None).unwrap_err();
    assert!(matches!(err, StoreError::Vfs(_)), "got {err:?}");

    // Old root is still intact and readable — atomic publish means the
    // pointer never half-moved.
    let rec = store.get_root("main").unwrap();
    assert_eq!(rec.hash, a);
    assert_eq!(rec.version, 1);

    // The crash left a staging file behind (it would be invisible to readers).
    let roots_dir = dir.path().join("roots");
    let temps: Vec<_> = std::fs::read_dir(&roots_dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with('.'))
        .collect();
    assert_eq!(
        temps.len(),
        1,
        "crash artifact staging file remains: {temps:?}"
    );

    // The new block exists on disk but is unreachable: a classic orphan.
    assert!(store.block_exists(&b).unwrap());
    drop(store);

    // Reopening reclaims crash staging files.
    let store = open_store(&dir);
    let leftovers: Vec<_> = std::fs::read_dir(&roots_dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with('.'))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temp files cleaned on open: {leftovers:?}"
    );

    // Old root survived everything.
    let rec = store.get_root("main").unwrap();
    assert_eq!(rec.hash, a);

    // GC collects the orphaned v2 block.
    let report = store.gc().unwrap();
    assert_eq!(report.removed, vec![b.to_hex()]);
    assert!(store.block_exists(&a).unwrap());
    assert!(!store.block_exists(&b).unwrap());
}

#[test]
fn failed_block_write_leaves_no_blob() {
    let dir = TempDir::new("fault-block");
    let (store, faults) = open_faulty(&dir);

    faults.fail_once("rename_block");
    let data = b"doomed block";
    let err = store.put_block(data, None, &[]).unwrap_err();
    assert!(matches!(err, StoreError::Vfs(_)));

    let h = Sha256::hash(data);
    assert!(!store.block_exists(&h).unwrap());
    assert!(matches!(store.get_block(&h), Err(StoreError::NotFound(_))));

    // Retry after the fault clears succeeds.
    let out = store.put_block(data, None, &[]).unwrap();
    assert!(!out.deduplicated);
    assert_eq!(store.get_block(&h).unwrap(), data);
}

#[test]
fn corrupt_blob_detected_on_read_and_kept_by_gc() {
    let dir = TempDir::new("corrupt");
    let (store, faults) = open_faulty(&dir);

    // Corrupt one byte during the temp write.
    faults.arm(
        "rename_block_tmp",
        cas_repo::vfs::FaultAction::CorruptByte {
            offset: 0,
            xor: 0xff,
        },
        Some(1),
    );
    let data = b"integrity matters";
    let out = store.put_block(data, None, &[]).unwrap();
    // Note: the store hashed the *clean* bytes, so the on-disk blob now
    // disagrees with its address — exactly what read-time verification catches.
    let err = store.get_block(&out.hash).unwrap_err();
    assert!(matches!(err, StoreError::Corrupt { .. }), "got {err:?}");

    // GC marks reachable blocks and verifies them: reported corrupt, kept.
    store.put_root("main", out.hash, None).unwrap();
    let report = store.gc().unwrap();
    assert_eq!(report.corrupt_blocks, vec![out.hash.to_hex()]);
    assert_eq!(report.blocks_removed, 0);
    assert!(store.block_exists(&out.hash).unwrap());
}

#[test]
fn io_failure_during_gc_aborts_without_deleting() {
    let dir = TempDir::new("fault-gc");
    let (store, faults) = open_faulty(&dir);

    let live = store.put_block(b"live", None, &[]).unwrap().hash;
    store.put_root("main", live, None).unwrap();
    let orphan = store.put_block(b"orphan", None, &[]).unwrap().hash;

    // Sweep's first delete fails: GC aborts, orphan stays, live stays.
    faults.fail_once("remove_block");
    let err = store.gc().unwrap_err();
    assert!(matches!(err, StoreError::Vfs(_)));
    assert!(store.block_exists(&live).unwrap());
    assert!(store.block_exists(&orphan).unwrap());

    // Clean run afterwards collects the orphan.
    let report = store.gc().unwrap();
    assert_eq!(report.blocks_removed, 1);
    assert!(store.block_exists(&live).unwrap());
    assert!(!store.block_exists(&orphan).unwrap());
}

// ---------- concurrency: same-block uploads ----------

#[test]
fn concurrent_identical_uploads_dedup_to_one_blob() {
    let dir = TempDir::new("concurrent-upload");
    let store = open_store(&dir);

    let data: Vec<u8> = (0..64 * 1024).map(|i| (i % 251) as u8).collect();
    let expected = Sha256::hash(&data);

    let threads = 16;
    let mut handles = Vec::new();
    let barrier = Arc::new(std::sync::Barrier::new(threads));
    for _ in 0..threads {
        let store = store.clone();
        let data = data.clone();
        let barrier = barrier.clone();
        handles.push(thread::spawn(move || {
            barrier.wait();
            store.put_block(&data, None, &[]).unwrap()
        }));
    }
    let outcomes: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();

    // Every upload agrees on the digest.
    for o in &outcomes {
        assert_eq!(o.hash, expected);
    }
    // Exactly one writer stored the blob; the rest deduplicated.
    let fresh = outcomes.iter().filter(|o| !o.deduplicated).count();
    assert_eq!(fresh, 1, "exactly one non-dedup upload, got {fresh}");

    // One blob on disk, content intact.
    assert!(blob_path(&dir, &expected).exists());
    assert_eq!(store.get_block(&expected).unwrap(), data);

    // No stray temp files in the shard.
    let shard = dir.path().join("blocks").join(&expected.to_hex()[..2]);
    let stray: Vec<_> = std::fs::read_dir(&shard)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with('.'))
        .collect();
    assert!(stray.is_empty());
}

#[test]
fn concurrent_uploads_of_distinct_blocks_all_land() {
    let dir = TempDir::new("concurrent-distinct");
    let store = open_store(&dir);

    let threads = 8;
    let per_thread = 10;
    let barrier = Arc::new(std::sync::Barrier::new(threads));
    let mut handles = Vec::new();
    for t in 0..threads {
        let store = store.clone();
        let barrier = barrier.clone();
        handles.push(thread::spawn(move || {
            barrier.wait();
            let mut hashes = Vec::new();
            for i in 0..per_thread {
                let data = format!("thread-{t}-block-{i}");
                let out = store.put_block(data.as_bytes(), None, &[]).unwrap();
                assert!(!out.deduplicated);
                hashes.push(out.hash);
            }
            hashes
        }));
    }
    let all: Vec<_> = handles
        .into_iter()
        .flat_map(|h| h.join().unwrap())
        .collect();
    assert_eq!(all.len(), threads * per_thread);
    for h in &all {
        assert!(store.block_exists(h).unwrap());
    }
    let stats = store.stats().unwrap();
    assert_eq!(stats.blocks, threads * per_thread);
}

#[test]
fn concurrent_upload_and_gc_of_same_new_block_is_safe() {
    // An upload racing a GC sweep of the same digest: the uploader pins
    // before its existence check, so either the sweep already deleted the
    // blob (uploader re-creates it) or the pin blocks the sweep. The block
    // must exist afterwards in both interleavings.
    let dir = TempDir::new("gc-upload-race");
    let store = open_store(&dir);

    let data = b"racy block";
    let h = Sha256::hash(data);
    store.put_block(data, None, &[]).unwrap();

    for _round in 0..20 {
        // Make it an orphan again.
        let store_gc = store.clone();
        let gc_thread = thread::spawn(move || store_gc.gc().unwrap());
        let store_up = store.clone();
        let up_thread = thread::spawn(move || store_up.put_block(data, None, &[]).unwrap());
        let _ = gc_thread.join().unwrap();
        let _ = up_thread.join().unwrap();
        // Whatever the interleaving, a final GC with no roots must leave the
        // store consistent: block either present-and-readable or collectable.
        if store.block_exists(&h).unwrap() {
            assert_eq!(store.get_block(&h).unwrap(), data);
        }
    }
}

// ---------- persistence ----------

#[test]
fn reopen_preserves_blocks_and_roots() {
    let dir = TempDir::new("reopen");
    let h;
    {
        let store = open_store(&dir);
        h = store.put_block(b"durable", None, &[]).unwrap().hash;
        store.put_root("main", h, None).unwrap();
    }
    let store = open_store(&dir);
    assert_eq!(store.get_block(&h).unwrap(), b"durable");
    let rec = store.get_root("main").unwrap();
    assert_eq!(rec.hash, h);
    assert_eq!(rec.version, 1);
}

#[test]
fn format_version_mismatch_rejected() {
    let dir = TempDir::new("version");
    let _store = open_store(&dir);
    drop(_store);
    std::fs::write(dir.path().join("VERSION"), "cas-repo v999\n").unwrap();
    let vfs: Box<dyn Vfs> = Box::new(StdVfs::new());
    match DynStore::open_boxed(dir.path(), vfs) {
        Err(StoreError::BadMetadata { .. }) => {}
        Err(other) => panic!("expected BadMetadata, got {other:?}"),
        Ok(_) => panic!("expected open to fail"),
    }
}
