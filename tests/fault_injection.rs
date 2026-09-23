//! Failure-injection acceptance tests using the injectable I/O layer:
//!
//! - [`mvcc_reclaim::io::FaultIo`] fails a chosen operation (write/sync/rename)
//!   after an observed operation count, returning an I/O error to the engine;
//! - [`mvcc_reclaim::io::SimCrashIo`] simulates power loss by truncating every
//!   file back to its last fsynced length.
//!
//! These verify: unpublished commits are not visible after reopening; torn
//! tails are repaired; a failed GC rename leaves the old log fully intact; an
//! injected fsync failure at publication is reported, not silently committed.

mod common;

use std::fs;
use std::io::{Seek as _, SeekFrom, Write as _};
use std::path::Path;
use std::sync::Arc;

use mvcc_reclaim::io::{FaultIo, FaultKind, FaultRule, SimCrashIo, StdIo};
use mvcc_reclaim::mvcc::Engine;
use mvcc_reclaim::store::{
    commit_frame_bytes, del_frame_bytes, parse_bytes, put_frame_bytes, HEADER_LEN, MAGIC,
};

fn read_log(dir: &Path) -> Vec<u8> {
    fs::read(dir.join("mvcc.log")).unwrap()
}

#[test]
fn torn_tail_is_truncated_and_never_becomes_committed() {
    let dir = common::temp_dir("torn");

    // Commit v1 normally.
    let db = Engine::open(&dir).unwrap();
    let mut w = db.begin_write();
    w.put("a", "1");
    w.commit().unwrap();
    drop(db);

    let mut good = read_log(&dir);
    let good_len = good.len() as u64;

    // Append a *complete* v2 data record but NO CMMT (crash before publish),
    // then a few torn garbage bytes.
    let put = put_frame_bytes(2, b"a", b"2");
    let rec_off = good.len() as u64;
    good.extend_from_slice(&put);
    good.extend_from_slice(b"\x00\x01garbage"); // torn frame
    fs::write(dir.join("mvcc.log"), &good).unwrap();

    // Reopen: torn tail truncated, uncommitted PUTV removed too (it has no
    // CMMT and sits beyond the last valid frame boundary of the CMMT? No — the
    // PUTV itself is a valid frame; the garbage is the torn part. The PUTV
    // remains as dead bytes but is NOT a committed version.)
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 1);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"1"[..]));

    // The valid prefix ends right after the torn garbage was discarded: i.e.
    // at the end of the complete-but-uncommitted PUTV.
    let buf = read_log(&dir);
    assert_eq!(buf.len() as u64, rec_off + put.len() as u64);
    let parsed = parse_bytes(&buf).unwrap();
    assert!(!parsed.torn);
    assert_eq!(parsed.versions, vec![1]);
    assert_eq!(parsed.records.len(), 2); // v1 and dead v2 record
    assert!(good_len > 0);
}

#[test]
fn simulated_power_loss_discards_unsynced_commit() {
    let dir = common::temp_dir("crash");
    let crash = Arc::new(SimCrashIo::new(Arc::new(StdIo)));

    // v1 durable.
    let db = Engine::open_with_io(crash.clone() as Arc<dyn mvcc_reclaim::io::Io>, &dir).unwrap();
    let mut w = db.begin_write();
    w.put("a", "durable");
    w.commit().unwrap();

    // Hand-craft a fully-written but unsynced v2 (data + CMMT), exactly as the
    // page cache would hold it after a crash: append to the underlying file,
    // but the simulator's last-synced length stays at the v1 boundary.
    {
        let synced = db.stats().file_bytes; // post-v1 length (handle was synced)
        let _ = synced;
    }
    let v1_len = {
        let parsed = parse_bytes(&read_log(&dir)).unwrap();
        parsed.valid_len
    };
    let data = put_frame_bytes(2, b"a", b"lost");
    let cmmt = commit_frame_bytes(2, v1_len);
    let mut f = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(dir.join("mvcc.log"))
        .unwrap();
    f.seek(SeekFrom::End(0)).unwrap();
    f.write_all(&data).unwrap();
    f.write_all(&cmmt).unwrap();
    // Crucially: no fsync, and the simulator did not register this handle.
    drop(f);

    // Power loss: truncate back to the last synced length.
    crash.crash();

    let buf = read_log(&dir);
    assert_eq!(buf.len() as u64, v1_len);

    let db2 = Engine::open_with_io(crash.clone() as Arc<dyn mvcc_reclaim::io::Io>, &dir).unwrap();
    assert_eq!(db2.latest_version(), 1);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"durable"[..]));
}

#[test]
fn injected_sync_failure_at_publish_is_not_committed() {
    let dir = common::temp_dir("syncfail");

    // Stack: real disk <- SimCrashIo (tracks last-synced lengths) <- FaultIo
    // (fails the publish fsync). A failed fsync models power loss, so after
    // the error we trigger crash() to discard unsynced bytes.
    let crash = Arc::new(SimCrashIo::new(Arc::new(StdIo)));
    let rules = vec![FaultRule::new(FaultKind::Sync, 2)]; // 1 = header sync
    let io = Arc::new(FaultIo::new(crash.clone() as Arc<dyn mvcc_reclaim::io::Io>, rules));
    let db = Engine::open_with_io(io, &dir).unwrap();

    let mut w = db.begin_write();
    w.put("a", "1");
    let res = w.commit();
    assert!(res.is_err(), "commit must surface the fsync error");

    // Power loss at the failed sync: unsynced data+CMMT vanish.
    crash.crash();
    drop(db);

    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 0);
    assert_eq!(db2.get_latest(b"a"), None);

    // A later successful commit allocates version 1 and works fine.
    let mut w = db2.begin_write();
    w.put("a", "real-v1");
    assert_eq!(w.commit().unwrap(), 1);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"real-v1"[..]));
}

#[test]
fn failed_gc_rename_keeps_old_log_intact_and_working() {
    let dir = common::temp_dir("gcrename");
    let db = Engine::open(&dir).unwrap();
    for i in 0..6u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }
    drop(db);

    // Rename count during GC: first rename is `log -> old`. Fail it.
    let rules = vec![FaultRule::new(FaultKind::Rename, 1)];
    let io = Arc::new(FaultIo::new(Arc::new(StdIo), rules));
    let db = Engine::open_with_io(io, &dir).unwrap();
    let res = db.gc();
    assert!(res.is_err(), "GC must surface the rename failure");

    // Old log is untouched and immediately usable.
    drop(db);
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 6);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"5"[..]));
    // Stale temp file from the failed GC must be cleaned up on open.
    let mut temps = 0;
    for e in fs::read_dir(&dir).unwrap().flatten() {
        if e.file_name().to_string_lossy().contains(".tmp.") {
            temps += 1;
        }
    }
    assert_eq!(temps, 0);
}

#[test]
fn failed_second_gc_rename_recovers_on_reopen() {
    let dir = common::temp_dir("gcrename2");
    let db = Engine::open(&dir).unwrap();
    for i in 0..4u64 {
        let mut w = db.begin_write();
        w.put("a", i.to_string());
        w.commit().unwrap();
    }
    drop(db);

    // Two renames occur in one GC (log->old, tmp->log). Fail the second one.
    let rules = vec![FaultRule::new(FaultKind::Rename, 2)];
    let io = Arc::new(FaultIo::new(Arc::new(StdIo), rules));
    let db = Engine::open_with_io(io, &dir).unwrap();
    assert!(db.gc().is_err());
    drop(db);

    // Recovery point after first rename: mvcc.old exists (the previous log),
    // and mvcc.log is missing. Open must rename old back to log.
    assert!(!dir.join("mvcc.log").exists());
    assert!(dir.join("mvcc.old").exists());
    let db2 = Engine::open(&dir).unwrap();
    assert_eq!(db2.latest_version(), 4);
    assert_eq!(db2.get_latest(b"a").as_deref(), Some(&b"3"[..]));
    assert!(dir.join("mvcc.log").exists());
    assert!(!dir.join("mvcc.old").exists());
}

#[test]
fn malformed_magic_is_rejected() {
    let dir = common::temp_dir("magic");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("mvcc.log"), b"NOTMVCCDATA").unwrap();
    assert!(Engine::open(&dir).is_err());
}

#[test]
fn frame_roundtrip_includes_crc_and_version() {
    let put = put_frame_bytes(7, b"k", b"v");
    let parsed = parse_bytes(&{
        let mut b = MAGIC.to_vec();
        b.push(1);
        b.extend_from_slice(&put);
        b
    })
    .unwrap();
    assert_eq!(parsed.records.len(), 1);
    assert_eq!(parsed.records[0].version, 7);
    assert_eq!(parsed.records[0].key, b"k");
    assert_eq!(parsed.records[0].value.as_deref(), Some(&b"v"[..]));

    // Corrupting one payload byte fails CRC => torn, zero valid records.
    let mut bad = MAGIC.to_vec();
    bad.push(1);
    let mut frame = put.clone();
    let n = frame.len();
    frame[n - 8] ^= 0xFF;
    bad.extend_from_slice(&frame);
    let p = parse_bytes(&bad).unwrap();
    assert!(p.torn);
    assert_eq!(p.records.len(), 0);
    assert_eq!(p.valid_len, HEADER_LEN);
}

#[test]
fn delete_frame_roundtrip() {
    let mut b = MAGIC.to_vec();
    b.push(1);
    b.extend_from_slice(&del_frame_bytes(3, b"gone"));
    let parsed = parse_bytes(&b).unwrap();
    assert_eq!(parsed.records[0].version, 3);
    assert!(parsed.records[0].value.is_none());
}
