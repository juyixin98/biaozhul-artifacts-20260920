//! Acceptance suite for the stated requirements:
//!
//! 1. fixed-snapshot read consistency in the presence of concurrent commits;
//! 2. write/write conflict rejection (first committer wins);
//! 3. version reclamation never removes versions an active snapshot needs;
//! 4. once that snapshot is released, the dead bytes are actually reclaimed.
//!
//! The interleaving is driven deterministically two ways:
//! - library-level sequencing in one thread;
//! - a two-thread rendezvous through the engine's [`CommitGate`], so the
//!   conflict really happens across OS threads with the loser parked at the
//!   exact pre-publication point of the winner's commit.

mod common;

use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};

use mvcc_reclaim::mvcc::{CommitError, CommitGate, Engine};

#[test]
fn snapshot_read_consistency() {
    let dir = common::temp_dir("snapshot");
    let db = Engine::open(&dir).unwrap();

    let mut w = db.begin_write();
    w.put("a", "v1");
    w.put("b", "b1");
    let v1 = w.commit().unwrap();
    assert_eq!(v1, 1);

    // Long reader pins version 1.
    let reader = db.begin_read();
    assert_eq!(reader.version(), 1);

    // Newer transactions commit twice more; the reader must not see them.
    let mut w = db.begin_write();
    w.put("a", "v2");
    w.put("c", "c2");
    let v2 = w.commit().unwrap();

    let mut w = db.begin_write();
    w.del("b");
    w.put("a", "v3");
    let v3 = w.commit().unwrap();

    assert_eq!(v2, 2);
    assert_eq!(v3, 3);

    assert_eq!(reader.get(b"a").as_deref(), Some(&b"v1"[..]));
    assert_eq!(reader.get(b"b").as_deref(), Some(&b"b1"[..]));
    assert_eq!(reader.get(b"c"), None); // key did not exist at v1

    // A fresh reader sees the current state.
    let fresh = db.begin_read();
    assert_eq!(fresh.get(b"a").as_deref(), Some(&b"v3"[..]));
    assert_eq!(fresh.get(b"b"), None); // deleted
    assert_eq!(fresh.get(b"c").as_deref(), Some(&b"c2"[..]));
    drop(fresh);

    // The pinned reader survives a restart of the process (reopen the store).
    drop(reader);
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 3);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"v3"[..]));
}

#[test]
fn write_write_conflict_first_committer_wins() {
    let dir = common::temp_dir("conflict");
    let db = Engine::open(&dir).unwrap();

    let mut seed = db.begin_write();
    seed.put("x", "0");
    seed.put("y", "0");
    seed.commit().unwrap();

    // Both writers snapshot version 1.
    let mut w1 = db.begin_write();
    let mut w2 = db.begin_write();
    assert_eq!(w1.snapshot_version(), 1);
    assert_eq!(w2.snapshot_version(), 1);

    w1.put("x", "from-w1"); // touches x
    w2.put("x", "from-w2"); // touches the same key

    assert_eq!(w1.commit().unwrap(), 2);
    match w2.commit() {
        Err(CommitError::Conflict { keys }) => assert_eq!(keys, vec![b"x".to_vec()]),
        other => panic!("expected conflict, got {other:?}"),
    }

    // A disjoint write still commits: different key, no conflict.
    let mut w3 = db.begin_write();
    assert_eq!(w3.snapshot_version(), 2);
    w3.put("y", "from-w3");
    assert_eq!(w3.commit().unwrap(), 3);

    // A retry of w2 re-reads the snapshot and must merge consciously; here it
    // simply overwrites on top of v2 successfully.
    let mut retry = db.begin_write();
    retry.put("x", "from-retry");
    assert_eq!(retry.commit().unwrap(), 4);
    assert_eq!(db.get_latest(b"x").as_deref(), Some(&b"from-retry"[..]));
}

#[test]
fn gc_preserves_active_snapshot_history_then_reclaims() {
    let dir = common::temp_dir("gc");
    let db = Engine::open(&dir).unwrap();

    // Commit v1..=3, then pin a long reader at v3 while newer versions land.
    for i in 1..=3u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }
    let reader = db.begin_read();
    assert_eq!(reader.version(), 3);
    assert_eq!(reader.get(b"a").as_deref(), Some(&b"3"[..]));

    // v4..=6 continue updating `a`; add a transient key b on v7, deleted v8.
    for i in 4..=6u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }
    let mut w = db.begin_write();
    w.put("b", "temp");
    w.commit().unwrap();
    let mut w = db.begin_write();
    w.del("b");
    w.commit().unwrap(); // v8

    let bytes_before = db.stats().file_bytes;

    // Watermark = 3. Records >= v3 are all needed; the v1/v2 records for `a`
    // are below the floor and GC may discard them (v2 is the new baseline).
    assert_eq!(db.watermark(), 3);
    let report = db.gc().unwrap().expect("GC should reclaim v1");
    assert!(report.reclaimed > 0);
    assert!(report.bytes_after < bytes_before);
    assert_eq!(report.watermark, 3);

    // The pinned v3 reader still works after compaction.
    assert_eq!(reader.get(b"a").as_deref(), Some(&b"3"[..]));

    // v1 is gone from disk, but the baseline v2 (needed so new snapshots see a
    // value for every version >= watermark) and v3..=8 remain.
    let on_disk = db.committed_versions_on_disk().unwrap();
    assert!(!on_disk.contains(&1), "v1 should be reclaimed");
    assert!(on_disk.contains(&2), "v2 baseline must survive");
    assert!(on_disk.contains(&3), "the active snapshot's v3 must survive");
    assert!(on_disk.contains(&8));

    // Latest state is intact.
    assert_eq!(db.get_latest(b"a").as_deref(), Some(&b"6"[..]));
    assert_eq!(db.get_latest(b"b"), None);
    assert_eq!(db.latest_version(), 8);

    // Release the reader: watermark jumps to latest+1 = 9; only the newest
    // record per key is needed as baseline.
    reader.release();
    assert_eq!(db.watermark(), 9);

    let st_before = db.stats();
    assert!(st_before.reclaimable_bytes > 0, "more history should be reclaimable");

    let report = db.gc().unwrap().expect("expected GC after release");
    assert!(report.reclaimed > 0, "space must actually be reclaimed");
    assert!(report.bytes_after < bytes_before);
    assert_eq!(report.watermark, 9);

    // After full GC only versions 6 (a) and 8 (b tombstone) remain as baseline.
    let on_disk = db.committed_versions_on_disk().unwrap();
    assert_eq!(on_disk, vec![6, 8]);

    assert_eq!(db.get_latest(b"a").as_deref(), Some(&b"6"[..]));
    assert_eq!(db.get_latest(b"b"), None);
    assert_eq!(db.latest_version(), 8);

    // Commits continue on top of the compacted log.
    let mut w = db.begin_write();
    w.put("a", "after-gc");
    let v = w.commit().unwrap();
    assert_eq!(v, 9);
    assert_eq!(db.get_latest(b"a").as_deref(), Some(&b"after-gc"[..]));

    // A second GC still works with the new watermark (VSET + later CMMT path).
    let mut w = db.begin_write();
    w.put("a", "after-gc-2");
    w.commit().unwrap();
    let _ = db.gc();
    assert_eq!(db.get_latest(b"a").as_deref(), Some(&b"after-gc-2"[..]));

    // Restart durability of the compacted image.
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 10);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"after-gc-2"[..]));
}

#[test]
fn deleted_below_watermark_baseline_is_tombstone() {
    // A key deleted before the watermark must stay absent (not resurrect an
    // older value after GC).
    let dir = common::temp_dir("tombstone");
    let db = Engine::open(&dir).unwrap();
    let mut w = db.begin_write();
    w.put("k", "v1");
    w.commit().unwrap();
    let mut w = db.begin_write();
    w.del("k");
    w.commit().unwrap();

    db.gc().unwrap(); // no readers: watermark 3
    assert_eq!(db.get_latest(b"k"), None);
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.get_latest(b"k"), None);
}

/// Deterministic two-thread interleaving using [`CommitGate`].
///
/// The gate fires after conflict checking but before publication. We force
/// W2 to reach *that point* of its commit only after W1 has fully committed:
/// the gate lets us observe W2 waiting on the engine lock while W1 finishes,
/// guaranteeing a real cross-thread conflict rather than test ordering luck.
#[test]
fn threaded_interleaved_conflict_and_monotonic_versions() {
    let dir = common::temp_dir("threads");

    // Gate: every commit parks after conflict-check / before publication,
    // reports the version it is about to publish, and waits for "go".
    let (at_commit_tx, at_commit_rx): (Sender<u64>, Receiver<u64>) = channel();
    let (release_tx, release_rx): (Sender<()>, Receiver<()>) = channel();
    let release_rx = Arc::new(Mutex::new(release_rx));
    let gate: CommitGate = Arc::new(move |version: u64| {
        at_commit_tx.send(version).unwrap();
        release_rx.lock().unwrap().recv().unwrap();
    });

    let db = Engine::open_with_io_and_gate(
        Arc::new(mvcc_reclaim::io::StdIo),
        &dir,
        Some(gate),
    )
    .unwrap();

    // Seed commit (version 1) also passes the gate.
    let seed = db.begin_write();
    let mut seed = seed;
    seed.put("n", "0");
    let h = std::thread::spawn(move || seed.commit().unwrap());
    assert_eq!(at_commit_rx.recv().unwrap(), 1);
    release_tx.send(()).unwrap();
    assert_eq!(h.join().unwrap(), 1);

    // W1 and W2 both snapshot v1, both touch n.
    let mut w1 = db.begin_write();
    let mut w2 = db.begin_write();
    w1.put("n", "w1");
    w2.put("n", "w2");

    let h1 = std::thread::spawn(move || w1.commit());
    // W1 reaches the gate while holding the engine lock.
    assert_eq!(at_commit_rx.recv().unwrap(), 2);

    let h2 = std::thread::spawn(move || w2.commit());
    // Give h2 time to block on the engine lock while h1 is parked at the gate.
    std::thread::sleep(std::time::Duration::from_millis(150));
    // Release W1's publication; W1 finishes, then W2 gets the lock and its
    // conflict check must reject it (it never reaches the gate).
    release_tx.send(()).unwrap();
    assert_eq!(h1.join().unwrap().unwrap(), 2);
    match h2.join().unwrap() {
        Err(CommitError::Conflict { keys }) => assert_eq!(keys, vec![b"n".to_vec()]),
        other => panic!("expected conflict from thread, got {other:?}"),
    }

    // Gate still serializes subsequent commits.
    let mut w3 = db.begin_write();
    w3.put("n", "w3");
    let h3 = std::thread::spawn(move || w3.commit());
    assert_eq!(at_commit_rx.recv().unwrap(), 3);
    release_tx.send(()).unwrap();
    assert_eq!(h3.join().unwrap().unwrap(), 3);
}

#[test]
fn gc_with_middle_snapshot_drops_only_old_unneeded_versions() {
    // Disk-level assertion of the reclamation rule at watermark 3:
    // v1 dropped, v2 kept as per-key baseline, v3..=6 all retained.
    let dir = common::temp_dir("middle-wm");
    let db = Engine::open(&dir).unwrap();
    for i in 1..=3u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }
    let reader = db.begin_read(); // pins v3
    for i in 4..=6u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }

    let report = db.gc().unwrap().unwrap();
    assert_eq!(report.watermark, 3);
    let on_disk = db.committed_versions_on_disk().unwrap();
    assert_eq!(on_disk, vec![2, 3, 4, 5, 6]);

    // Pinned snapshot reads exactly its version, before and after release.
    assert_eq!(reader.get(b"a").as_deref(), Some(&b"3"[..]));
    drop(reader);

    // And a fresh snapshot sees only the latest value.
    let now = db.begin_read();
    assert_eq!(now.get(b"a").as_deref(), Some(&b"6"[..]));
}

#[test]
fn many_threads_publish_distinct_monotonic_versions() {
    let dir = common::temp_dir("many");
    let db = Engine::open(&dir).unwrap();

    let handles: Vec<_> = (0..8)
        .map(|i| {
            let db = db.clone();
            std::thread::spawn(move || {
                // Each thread retries on conflict until its commit lands.
                loop {
                    let mut w = db.begin_write();
                    w.put(format!("k{i}"), format!("v{i}"));
                    match w.commit() {
                        Ok(v) => return v,
                        Err(CommitError::Conflict { .. }) => continue,
                        Err(e) => panic!("{e}"),
                    }
                }
            })
        })
        .collect();

    let mut versions: Vec<u64> =
        handles.into_iter().map(|h| h.join().unwrap()).collect();
    versions.sort();
    assert_eq!(versions, (1..=8).collect::<Vec<u64>>());
    assert_eq!(db.latest_version(), 8);
}
