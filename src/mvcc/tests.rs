//! 引擎层测试：快照读、墓碑、写写冲突、GC 安全性。

use super::*;

fn k(s: &str) -> Vec<u8> {
    s.as_bytes().to_vec()
}

#[test]
fn snapshot_read_ignores_later_commits() {
    let db = MvccStore::new();
    // 初始写入 v1。
    let t = db.begin();
    db.put(t.0, k("a"), b"v1".to_vec()).unwrap();
    db.commit(t.0).unwrap();

    // 长读事务开始于 v1 之后。
    let reader = db.begin();
    assert_eq!(db.get(reader.0, &k("a")).unwrap(), Some(b"v1".to_vec()));

    // 另一事务覆盖为 v2、v3 并删除。
    for val in [b"v2".as_ref(), b"v3".as_ref()] {
        let w = db.begin();
        db.put(w.0, k("a"), val.to_vec()).unwrap();
        db.commit(w.0).unwrap();
    }
    let d = db.begin();
    db.delete(d.0, k("a")).unwrap();
    db.commit(d.0).unwrap();

    // 旧读不变。
    assert_eq!(db.get(reader.0, &k("a")).unwrap(), Some(b"v1".to_vec()));
    // 新读看到删除。
    let after = db.begin();
    assert_eq!(db.get(after.0, &k("a")).unwrap(), None);
    db.rollback(reader.0);
    db.rollback(after.0);
}

#[test]
fn read_your_own_writes() {
    let db = MvccStore::new();
    let t = db.begin();
    db.put(t.0, k("x"), b"1".to_vec()).unwrap();
    assert_eq!(db.get(t.0, &k("x")).unwrap(), Some(b"1".to_vec()));
    db.delete(t.0, k("x")).unwrap();
    assert_eq!(db.get(t.0, &k("x")).unwrap(), None);
    db.rollback(t.0);

    let t2 = db.begin();
    assert_eq!(db.get(t2.0, &k("x")).unwrap(), None);
    db.rollback(t2.0);
}

#[test]
fn concurrent_writers_only_one_commits() {
    let db = MvccStore::new();
    // 初始版本。
    let init = db.begin();
    db.put(init.0, k("k"), b"0".to_vec()).unwrap();
    db.commit(init.0).unwrap();

    // 两个事务在同一快照点之后并发写同一键。
    let t1 = db.begin();
    let t2 = db.begin();
    assert_eq!(t1.1, t2.1);

    db.put(t1.0, k("k"), b"a".to_vec()).unwrap();
    db.put(t2.0, k("k"), b"b".to_vec()).unwrap();

    // t1 先提交成功。
    assert!(db.commit(t1.0).is_ok());
    // t2 提交检测到写-写冲突，中止。
    let err = db.commit(t2.0).unwrap_err();
    assert!(matches!(
        err,
        CommitError::WriteWriteConflict { key } if key == k("k")
    ));

    // 落库的是 t1 的值。
    let t3 = db.begin();
    assert_eq!(db.get(t3.0, &k("k")).unwrap(), Some(b"a".to_vec()));
    db.rollback(t3.0);
}

#[test]
fn non_overlapping_keys_both_commit() {
    let db = MvccStore::new();
    let t1 = db.begin();
    let t2 = db.begin();
    db.put(t1.0, k("a"), b"1".to_vec()).unwrap();
    db.put(t2.0, k("b"), b"2".to_vec()).unwrap();
    assert!(db.commit(t1.0).is_ok());
    assert!(db.commit(t2.0).is_ok());
}

#[test]
fn gc_blocked_by_open_snapshot_then_allowed_after_close() {
    let db = MvccStore::new();
    // v1
    let t = db.begin();
    db.put(t.0, k("a"), b"v1".to_vec()).unwrap();
    db.commit(t.0).unwrap();

    // 长读事务停留在 v1。
    let reader = db.begin();

    // 三次覆盖 + 删除。
    for v in ["v2", "v3", "v4"] {
        let w = db.begin();
        db.put(w.0, k("a"), v.as_bytes().to_vec()).unwrap();
        db.commit(w.0).unwrap();
    }
    let d = db.begin();
    db.delete(d.0, k("a")).unwrap();
    db.commit(d.0).unwrap();

    assert_eq!(db.debug_versions(&k("a")).len(), 5);

    // 快照开着：GC 不能回收 v1（其 commit_ts 即水位线可见版本）。
    let stats = db.gc();
    assert_eq!(db.get(reader.0, &k("a")).unwrap(), Some(b"v1".to_vec()));
    assert_eq!(db.debug_versions(&k("a")).len(), 5);
    assert_eq!(stats.versions_reclaimed, 0);
    assert_eq!(stats.watermark, Some(reader.1));

    // 关闭快照后 GC：旧版本全部回收，仅剩墓碑，整条键删除。
    db.rollback(reader.0);
    assert_eq!(db.watermark(), None);
    let stats = db.gc();
    assert!(stats.versions_reclaimed >= 4);
    assert_eq!(stats.keys_removed, 1);
    assert!(db.debug_versions(&k("a")).is_empty());

    // 新读看到“不存在”，回收前后语义一致（对新事务而言本来就是删除）。
    let t = db.begin();
    assert_eq!(db.get(t.0, &k("a")).unwrap(), None);
    db.rollback(t.0);
}

#[test]
fn readonly_snapshot_pins_versions() {
    let db = MvccStore::new();
    let t = db.begin();
    db.put(t.0, k("a"), b"v1".to_vec()).unwrap();
    db.commit(t.0).unwrap();

    let (sid, snap_ts) = db.create_snapshot();

    let w = db.begin();
    db.put(w.0, k("a"), b"v2".to_vec()).unwrap();
    db.commit(w.0).unwrap();

    db.gc(); // 快照未关闭，v1 必须保留。
    assert_eq!(
        db.snapshot_read(sid, &k("a")).unwrap(),
        Some(b"v1".to_vec())
    );
    assert_eq!(db.debug_versions(&k("a")).len(), 2);

    assert!(db.close_snapshot(sid));
    assert_eq!(db.watermark(), None);
    db.gc();
    // v1 被回收，仅剩 v2。
    assert_eq!(
        db.debug_versions(&k("a")),
        vec![(2, false)]
    );
    let _ = snap_ts;
}

#[test]
fn gc_keeps_versions_newer_than_oldest_snapshot() {
    let db = MvccStore::new();
    // 先开一个停留在空库的快照（ts=0）。
    let old = db.begin();
    // 之后才创建键：两个版本。
    for v in ["v1", "v2"] {
        let w = db.begin();
        db.put(w.0, k("late"), v.as_bytes().to_vec()).unwrap();
        db.commit(w.0).unwrap();
    }
    // 水位线为 0，两个版本对该快照均不可见，但未来读需要 -> 全保留。
    let stats = db.gc();
    assert_eq!(stats.versions_reclaimed, 0);
    assert_eq!(db.debug_versions(&k("late")).len(), 2);
    db.rollback(old.0);
}

#[test]
fn conflict_does_not_poison_store() {
    let db = MvccStore::new();
    let i = db.begin();
    db.put(i.0, k("k"), b"0".to_vec()).unwrap();
    db.commit(i.0).unwrap();

    let t1 = db.begin();
    let t2 = db.begin();
    db.put(t1.0, k("k"), b"a".to_vec()).unwrap();
    db.put(t2.0, k("k"), b"b".to_vec()).unwrap();
    db.commit(t1.0).unwrap();
    assert!(db.commit(t2.0).is_err());

    // t2 中止后不应残留在事务表，也不影响后续提交。
    assert_eq!(db.debug_active_txn_count(), 0);
    let t3 = db.begin();
    db.put(t3.0, k("k"), b"c".to_vec()).unwrap();
    assert!(db.commit(t3.0).is_ok());
}

#[test]
fn duplicate_commit_or_rollback_is_not_found() {
    let db = MvccStore::new();
    let t = db.begin();
    db.put(t.0, k("a"), b"1".to_vec()).unwrap();
    assert!(db.commit(t.0).is_ok());
    assert_eq!(db.commit(t.0).err(), Some(CommitError::NotFound));
    assert!(!db.rollback(t.0));
}
