//! Engine behaviour tests on the in-memory file system.

use std::path::PathBuf;
use std::sync::Arc;

use mvcc_gc::engine::Engine;
use mvcc_gc::error::Error;
use mvcc_gc::vfs::MemVfs;

fn open() -> (Engine, MemVfs) {
    let vfs = MemVfs::new();
    let engine = Engine::open("/data", Arc::new(vfs.clone())).unwrap();
    (engine, vfs)
}

fn b(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}

#[test]
fn put_get_commit_and_versions_are_monotonic() {
    let (e, _vfs) = open();
    assert_eq!(e.current_version(), 0);

    let (t1, rv1) = e.begin();
    assert_eq!(rv1, 0);
    e.put(t1, b("a"), b("1")).unwrap();
    e.put(t1, b("b"), b("2")).unwrap();
    assert_eq!(e.commit(t1).unwrap(), 1);

    let (t2, rv2) = e.begin();
    assert_eq!(rv2, 1);
    e.put(t2, b("a"), b("3")).unwrap();
    assert_eq!(e.commit(t2).unwrap(), 2);

    let (t3, _) = e.begin();
    assert_eq!(e.get_tx(t3, b"a").unwrap(), Some(b("3")));
    assert_eq!(e.get_tx(t3, b"b").unwrap(), Some(b("2")));
    assert_eq!(e.get_tx(t3, b"missing").unwrap(), None);
}

#[test]
fn transaction_reads_its_own_writes_and_fixed_snapshot() {
    let (e, _vfs) = open();
    let (t1, _) = e.begin();
    e.put(t1, b("k"), b("v1")).unwrap();
    e.commit(t1).unwrap();

    // Long transaction starts at v1.
    let (reader, rv) = e.begin();
    assert_eq!(rv, 1);

    // Another transaction overwrites k.
    let (t2, _) = e.begin();
    e.put(t2, b("k"), b("v2")).unwrap();
    e.commit(t2).unwrap();

    // The long transaction still sees v1.
    assert_eq!(e.get_tx(reader, b"k").unwrap(), Some(b("v1")));

    // Read-your-writes inside the long transaction.
    e.put(reader, b("k"), b("mine")).unwrap();
    assert_eq!(e.get_tx(reader, b"k").unwrap(), Some(b("mine")));
    e.abort(reader).unwrap();

    // A fresh transaction sees v2.
    let (t3, _) = e.begin();
    assert_eq!(e.get_tx(t3, b"k").unwrap(), Some(b("v2")));
}

#[test]
fn write_write_conflict_rejects_second_commit() {
    let (e, _vfs) = open();
    let (t0, _) = e.begin();
    e.put(t0, b("x"), b("0")).unwrap();
    e.commit(t0).unwrap();

    // Two writers race on the same key from the same snapshot.
    let (a, _) = e.begin();
    let (btx, _) = e.begin();
    e.put(a, b("x"), b("A")).unwrap();
    e.put(btx, b("x"), b("B")).unwrap();

    e.commit(a).unwrap(); // first writer wins
    match e.commit(btx) {
        Err(Error::Conflict(k)) => assert_eq!(k, b("x")),
        other => panic!("expected conflict, got {other:?}"),
    }

    // The losing transaction is still open: abort it, retry as a new tx.
    e.abort(btx).unwrap();
    let (c, _) = e.begin();
    e.put(c, b("x"), b("B")).unwrap();
    e.commit(c).unwrap();

    let (t, _) = e.begin();
    assert_eq!(e.get_tx(t, b"x").unwrap(), Some(b("B")));
}

#[test]
fn disjoint_writers_do_not_conflict() {
    let (e, _vfs) = open();
    let (a, _) = e.begin();
    let (btx, _) = e.begin();
    e.put(a, b("k1"), b("A")).unwrap();
    e.put(btx, b("k2"), b("B")).unwrap();
    e.commit(a).unwrap();
    e.commit(btx).unwrap(); // no shared keys: both commit
}

#[test]
fn delete_removes_key_and_conflicts_like_a_write() {
    let (e, _vfs) = open();
    let (t1, _) = e.begin();
    e.put(t1, b("d"), b("v")).unwrap();
    e.commit(t1).unwrap();

    let (t2, _) = e.begin();
    e.delete(t2, b("d")).unwrap();
    e.commit(t2).unwrap();

    let (t3, _) = e.begin();
    assert_eq!(e.get_tx(t3, b"d").unwrap(), None);

    // Delete of a never-existing key commits fine.
    let (t4, _) = e.begin();
    e.delete(t4, b("never")).unwrap();
    e.commit(t4).unwrap();
}

#[test]
fn abort_discards_buffered_writes() {
    let (e, _vfs) = open();
    let (t, _) = e.begin();
    e.put(t, b("k"), b("v")).unwrap();
    e.abort(t).unwrap();
    assert!(matches!(e.commit(t), Err(Error::UnknownTx(_))));
    let (t2, _) = e.begin();
    assert_eq!(e.get_tx(t2, b"k").unwrap(), None);
}

#[test]
fn explicit_snapshot_pins_a_version() {
    let (e, _vfs) = open();
    let (t1, _) = e.begin();
    e.put(t1, b("s"), b("old")).unwrap();
    e.commit(t1).unwrap();

    let snap = e.snapshot();
    assert_eq!(snap.version, 1);

    let (t2, _) = e.begin();
    e.put(t2, b("s"), b("new")).unwrap();
    e.commit(t2).unwrap();

    assert_eq!(e.get_snapshot(snap.id, b"s").unwrap(), Some(b("old")));
    e.release_snapshot(snap.id).unwrap();
    assert!(matches!(
        e.get_snapshot(snap.id, b"s"),
        Err(Error::UnknownSnapshot(_))
    ));
}

#[test]
fn gc_is_noop_without_progress() {
    let (e, _vfs) = open();
    let report = e.gc().unwrap();
    assert!(!report.base_written);
    assert_eq!(report.files_removed, 0);
}

#[test]
fn gc_compacts_and_reclaims_when_no_readers() {
    let (e, vfs) = open();
    for i in 0..5 {
        let (t, _) = e.begin();
        e.put(t, b("k"), format!("v{i}").into_bytes()).unwrap();
        e.commit(t).unwrap();
    }
    let before = e.stats();
    assert_eq!(before.segment_files, 5);
    let bytes_before = vfs.bytes_under(&PathBuf::from("/data")).1;

    let report = e.gc().unwrap();
    assert!(report.base_written);
    assert_eq!(report.horizon, 5);
    assert_eq!(report.files_removed, 5);
    assert!(report.bytes_reclaimed > 0);

    let after = e.stats();
    assert_eq!(after.segment_files, 0);
    assert_eq!(after.base_files, 1);
    let bytes_after = vfs.bytes_under(&PathBuf::from("/data")).1;
    assert!(
        bytes_after < bytes_before,
        "expected reclamation: {bytes_before} -> {bytes_after}"
    );

    // Data still readable at the latest version.
    let (t, _) = e.begin();
    assert_eq!(e.get_tx(t, b"k").unwrap(), Some(b("v4")));
}

#[test]
fn gc_retains_versions_needed_by_long_reader() {
    let (e, vfs) = open();
    // v1: k=old
    let (t1, _) = e.begin();
    e.put(t1, b("k"), b("old")).unwrap();
    e.commit(t1).unwrap();

    // Long reader pins v1.
    let snap = e.snapshot();
    assert_eq!(snap.version, 1);

    // v2..=v4 overwrite k.
    for i in 2..=4u32 {
        let (t, _) = e.begin();
        e.put(t, b("k"), format!("v{i}").into_bytes()).unwrap();
        e.commit(t).unwrap();
    }

    // GC while the reader is pinned: horizon stays at 1.
    let report = e.gc().unwrap();
    assert_eq!(report.horizon, 1);
    // seg-1 is folded into the base; newer segments must survive.
    let stats = e.stats();
    assert_eq!(stats.base_files, 1);
    assert_eq!(stats.segment_files, 3, "segments 2..4 must be retained");

    // The pinned reader still reads the old value.
    assert_eq!(e.get_snapshot(snap.id, b"k").unwrap(), Some(b("old")));

    // Release the pin: the next GC compacts everything.
    e.release_snapshot(snap.id).unwrap();
    let report2 = e.gc().unwrap();
    assert_eq!(report2.horizon, 4);
    let stats2 = e.stats();
    assert_eq!(stats2.segment_files, 0);
    assert_eq!(stats2.base_files, 1);

    // Space actually reclaimed relative to the pinned state.
    let bytes_now = vfs.bytes_under(&PathBuf::from("/data")).1;
    assert!(bytes_now > 0);

    // Latest value still correct.
    let (t, _) = e.begin();
    assert_eq!(e.get_tx(t, b"k").unwrap(), Some(b("v4")));
}

#[test]
fn reopen_recovers_committed_state() {
    let vfs = MemVfs::new();
    {
        let e = Engine::open("/data", Arc::new(vfs.clone())).unwrap();
        let (t, _) = e.begin();
        e.put(t, b("persist"), b("yes")).unwrap();
        e.commit(t).unwrap();
        let (t2, _) = e.begin();
        e.put(t2, b("gone"), b("uncommitted")).unwrap();
        e.abort(t2).unwrap();
    } // engine dropped: LOCK released

    let e2 = Engine::open("/data", Arc::new(vfs.clone())).unwrap();
    assert_eq!(e2.current_version(), 1);
    let (t, _) = e2.begin();
    assert_eq!(e2.get_tx(t, b"persist").unwrap(), Some(b("yes")));
    assert_eq!(e2.get_tx(t, b"gone").unwrap(), None);
}

#[test]
fn reopen_after_gc_reads_from_base() {
    let vfs = MemVfs::new();
    {
        let e = Engine::open("/data", Arc::new(vfs.clone())).unwrap();
        for i in 0..3 {
            let (t, _) = e.begin();
            e.put(t, b("k"), format!("v{i}").into_bytes()).unwrap();
            e.commit(t).unwrap();
        }
        e.gc().unwrap();
    }
    let e2 = Engine::open("/data", Arc::new(vfs.clone())).unwrap();
    assert_eq!(e2.current_version(), 3);
    let (t, _) = e2.begin();
    assert_eq!(e2.get_tx(t, b"k").unwrap(), Some(b("v2")));
    // And new commits keep working on top of the base.
    e2.put(t, b("k"), b("v3")).unwrap();
    assert_eq!(e2.commit(t).unwrap(), 4);
}
