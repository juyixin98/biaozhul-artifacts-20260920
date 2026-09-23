//! Fault-injection and crash-boundary tests using [`FaultVfs`] over the
//! in-memory file system.

use std::sync::Arc;

use mvcc_gc::engine::Engine;
use mvcc_gc::error::Error;
use mvcc_gc::vfs::{FaultOp, FaultVfs, MemVfs};

fn b(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}

fn open_faulty(mem: &MemVfs) -> (Engine, FaultVfs) {
    let fault = FaultVfs::new(Arc::new(mem.clone()));
    let engine = Engine::open_faulted("/data", fault.clone()).unwrap();
    (engine, fault)
}

#[test]
fn commit_sync_failure_leaves_tx_open_and_retry_succeeds() {
    let mem = MemVfs::new();
    let (e, fault) = open_faulty(&mem);

    let (t, _) = e.begin();
    e.put(t, b("k"), b("v")).unwrap();
    // Make the commit's fsync barrier fail.
    fault.arm(FaultOp::Sync, false);
    match e.commit(t) {
        Err(Error::Injected(_)) | Err(Error::Io(_)) => {}
        other => panic!("expected injected failure, got {other:?}"),
    }

    // No version was published.
    assert_eq!(e.current_version(), 0);

    // Transaction is still usable: retry the commit without a fault.
    let v = e.commit(t).unwrap();
    assert_eq!(v, 1);
    assert_eq!(e.current_version(), 1);
    let (t2, _) = e.begin();
    assert_eq!(e.get_tx(t2, &b("k")).unwrap(), Some(b("v")));
}

#[test]
fn commit_rename_failure_does_not_publish_version_or_file() {
    let mem = MemVfs::new();
    let (e, fault) = open_faulty(&mem);
    let (t, _) = e.begin();
    e.put(t, b("k"), b("v")).unwrap();
    fault.arm(FaultOp::Rename, false); // file never reaches its final name
    assert!(e.commit(t).is_err());
    assert_eq!(e.current_version(), 0);

    // Simulate a restart: only temp debris is present and is cleaned up;
    // there is no committed segment to replay.
    drop(e);
    let e2 = Engine::open("/data", Arc::new(mem.clone())).unwrap();
    assert_eq!(e2.current_version(), 0);
    let (t2, _) = e2.begin();
    assert_eq!(e2.get_tx(t2, &b("k")).unwrap(), None);
    let names: Vec<String> = mem
        .list_paths()
        .into_iter()
        .filter_map(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .collect();
    assert!(!names.iter().any(|n| n.ends_with(".tmp")));
}

#[test]
fn injected_fault_is_one_shot() {
    let mem = MemVfs::new();
    let (e, fault) = open_faulty(&mem);
    fault.arm(FaultOp::Sync, false);
    let (t, _) = e.begin();
    e.put(t, b("a"), b("1")).unwrap();
    assert!(e.commit(t).is_err());
    assert!(
        fault.armed().is_none(),
        "fault must self-disarm after firing"
    );

    // No fault remains armed: later commits succeed normally.
    let (t2, _) = e.begin();
    e.put(t2, b("a"), b("2")).unwrap();
    assert_eq!(e.commit(t2).unwrap(), 1);
}

#[test]
fn gc_remove_failure_leaves_stale_file_retried_next_pass() {
    let mem = MemVfs::new();
    let (e, fault) = open_faulty(&mem);
    for i in 0..3u32 {
        let (t, _) = e.begin();
        e.put(t, b("k"), format!("v{i}").into_bytes()).unwrap();
        e.commit(t).unwrap();
    }

    // Fail one unlink during the first GC pass.
    fault.arm(FaultOp::Remove, false);
    let report = e.gc().unwrap();
    assert!(report.base_written);
    assert_eq!(report.stale_left, 1, "one obsolete file failed to unlink");

    let stats = e.stats();
    assert_eq!(stats.stale_files, 1);

    // Next pass has no newer horizon but retries the stale unlink.
    let report2 = e.gc().unwrap();
    assert!(!report2.base_written, "no newer horizon to compact");
    let stats2 = e.stats();
    assert_eq!(stats2.stale_files, 0);
}

#[test]
fn interrupted_gc_before_base_publish_deletes_nothing() {
    let mem = MemVfs::new();
    let (e, fault) = open_faulty(&mem);
    for i in 0..3u32 {
        let (t, _) = e.begin();
        e.put(t, b("k"), format!("v{i}").into_bytes()).unwrap();
        e.commit(t).unwrap();
    }

    // GC fails while fsyncing the new base temp file: no base, no deletes.
    fault.arm(FaultOp::Sync, false);
    assert!(e.gc().is_err());

    let stats = e.stats();
    assert_eq!(stats.base_version, 0);
    assert_eq!(
        stats.segment_files, 3,
        "nothing may be deleted before the base is durable"
    );

    // Disarm and rerun: normal compaction.
    fault.disarm();
    let report = e.gc().unwrap();
    assert!(report.base_written);
    assert_eq!(report.files_removed, 3);

    let (t, _) = e.begin();
    assert_eq!(e.get_tx(t, &b("k")).unwrap(), Some(b("v2")));
}

#[test]
fn reopen_cleans_temp_debris_after_crash() {
    let mem = MemVfs::new();
    let fault = FaultVfs::new(Arc::new(mem.clone()));
    let e = Engine::open_faulted("/data", fault.clone()).unwrap();
    let (t, _) = e.begin();
    e.put(t, b("k"), b("v")).unwrap();
    // Crash the commit mid-write, after a prefix of the temp file landed.
    fault.arm(FaultOp::Write, true);
    let _ = e.commit(t);

    assert!(
        mem.list_paths().iter().any(|p| p
            .file_name()
            .map(|n| n.to_string_lossy().starts_with('.') && n.to_string_lossy().ends_with(".tmp"))
            .unwrap_or(false)),
        "a torn temp file should be present after the crash"
    );

    drop(e);
    let e2 = Engine::open("/data", Arc::new(mem.clone())).unwrap();
    assert_eq!(e2.current_version(), 0);
    let names: Vec<String> = mem
        .list_paths()
        .into_iter()
        .filter_map(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .collect();
    assert!(!names.iter().any(|n| n.ends_with(".tmp")));
}
