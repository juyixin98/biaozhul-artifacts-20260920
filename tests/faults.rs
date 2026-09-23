//! Acceptance + failure-injection tests for the storage engine.
//!
//! These tests use the injectable I/O layer to simulate ENOSPC / EIO and
//! verify the documented sync boundary: a torn trailing record is detected
//! by its CRC and truncated on reopen, while all fully synced records survive.

use std::path::PathBuf;
use std::sync::Arc;

use tsblock::coding::Point;
use tsblock::io_layer::{FaultRules, FaultyIo, RealIo, EIO, ENOSPC};
use tsblock::store::{Store, UnorderedPolicy};

fn tempdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "tsblock-it-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn make_points(n: i64) -> Vec<Point> {
    // Timestamps every 60 s, values following a gentle integer walk with
    // occasional sign changes: exercises DOD, first-difference and zigzag.
    (0..n)
        .map(|i| Point::new(1_700_000_000 + i * 60, (i % 13) - 6))
        .collect()
}

#[test]
fn enospc_mid_record_leaves_truncatable_tail_and_loses_nothing_synced() {
    let dir = tempdir("enospc");

    // Phase 1: write two full, synced blocks before any fault is armed.
    let store = Store::open(&dir, Arc::new(RealIo::new()), 50).unwrap();
    store
        .create_series("cpu", UnorderedPolicy::Reject, Some(50))
        .unwrap();
    let first100 = make_points(100);
    let out = store.write("cpu", &first100).unwrap();
    assert_eq!(out.blocks_flushed, 2);
    drop(store);

    // Phase 2: arm a write budget that fails partway through the next record.
    let rules2 = Arc::new(FaultRules::new());
    let io2 = Arc::new(FaultyIo::new(Arc::new(RealIo::new()), Arc::clone(&rules2)));
    let store2 = Store::open(&dir, io2, 50).unwrap();
    // Allow only 20 bytes of the next record (record header is 8 bytes), so
    // the write is genuinely partial.
    rules2.fail_writes_after(20);
    let next = make_points(150)[100..150].to_vec();
    let err = store2.write("cpu", &next).unwrap_err();
    let raw = err.to_string();
    assert!(
        raw.contains(&ENOSPC.to_string()) || raw.contains("os error 28"),
        "expected ENOSPC, got {raw}"
    );

    // The failed points remain in the active block and can be retried after
    // the fault is cleared: durability is not silently claimed.
    rules2.clear();
    let out = store2.flush(Some("cpu")).unwrap();
    assert_eq!(out, 1);
    drop(store2);

    // Phase 3: reopen with a pristine backend; recovery must report no torn
    // record (the successful retry overwrote the torn region positionally).
    let store3 = Store::open(&dir, Arc::new(RealIo::new()), 50).unwrap();
    let rep = store3.recovery_report();
    assert_eq!(rep.torn_records, 0, "{rep:?}");
    assert_eq!(rep.blocks_loaded, 3);
    let all = store3.query("cpu", i64::MIN, i64::MAX).unwrap().points;
    assert_eq!(all.len(), 150);
    assert_eq!(all, make_points(150));
}

#[test]
fn torn_record_without_retry_is_truncated_on_reopen() {
    // Reproduce a crash after a partial write with no retry: directly truncate
    // the backing file mid-record, then reopen and check recovery truncation.
    let dir = tempdir("torn");
    let store = Store::open(&dir, Arc::new(RealIo::new()), 40).unwrap();
    store
        .create_series("m", UnorderedPolicy::Reject, Some(40))
        .unwrap();
    let pts = make_points(80);
    store.write("m", &pts).unwrap(); // 2 synced blocks
    drop(store);

    let path = dir.join("m.tsb");
    let len = std::fs::metadata(&path).unwrap().len();
    // Append a bogus record header and a few garbage payload bytes.
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        f.write_all(&500u32.to_le_bytes()).unwrap(); // claims 500 bytes
        f.write_all(&0xDEAD_BEEFu32.to_le_bytes()).unwrap();
        f.write_all(&[0xAB; 10]).unwrap();
    }
    assert_eq!(std::fs::metadata(&path).unwrap().len(), len + 18);

    let store2 = Store::open(&dir, Arc::new(RealIo::new()), 40).unwrap();
    let rep = store2.recovery_report();
    assert_eq!(rep.series_loaded, 1);
    assert_eq!(rep.blocks_loaded, 2);
    assert_eq!(rep.torn_records, 1);
    // File truncated back to the last valid record boundary.
    assert_eq!(std::fs::metadata(&path).unwrap().len(), len);
    let all = store2.query("m", i64::MIN, i64::MAX).unwrap().points;
    assert_eq!(all, pts);
}

#[test]
fn failed_fsync_is_surfaced_and_points_remain_retryable() {
    let dir = tempdir("eio");
    let rules = Arc::new(FaultRules::new());
    let io = Arc::new(FaultyIo::new(Arc::new(RealIo::new()), Arc::clone(&rules)));
    let store = Store::open(&dir, io, 10).unwrap();
    store
        .create_series("s", UnorderedPolicy::Reject, Some(10))
        .unwrap();

    // First block's write succeeds but its fsync fails with EIO.
    rules.fail_next_syncs(1);
    let err = store.write("s", &make_points(10)).unwrap_err();
    assert!(
        err.to_string().contains(&EIO.to_string()) || err.to_string().contains("os error 5"),
        "expected EIO, got {}",
        err
    );

    // Points are still active; after clearing the fault, flush persists them.
    rules.clear();
    assert_eq!(store.flush(Some("s")).unwrap(), 1);
    let q = store.query("s", i64::MIN, i64::MAX).unwrap();
    assert_eq!(q.points.len(), 10);

    // Reopen: even if the kernel had not kept the bytes, recovery never sees
    // more than one well-formed record here.
    drop(store);
    let store2 = Store::open(&dir, Arc::new(RealIo::new()), 10).unwrap();
    assert_eq!(
        store2.query("s", i64::MIN, i64::MAX).unwrap().points.len(),
        10
    );
}

#[test]
fn extreme_and_duplicate_values_compress_and_roundtrip() {
    let dir = tempdir("extreme");
    let store = Store::open(&dir, Arc::new(RealIo::new()), 1000).unwrap();
    store
        .create_series("x", UnorderedPolicy::Reject, Some(1000))
        .unwrap();
    let pts = vec![
        Point::new(i64::MIN, i64::MIN),
        Point::new(i64::MIN, 0), // duplicate ts
        Point::new(i64::MIN + 1, i64::MAX),
        Point::new(0, -1),
        Point::new(i64::MAX - 1, i64::MIN),
        Point::new(i64::MAX, i64::MAX),
    ];
    store.write("x", &pts).unwrap();
    store.flush(Some("x")).unwrap();
    let got = store.query("x", i64::MIN, i64::MAX).unwrap();
    assert_eq!(got.points, pts);
}
