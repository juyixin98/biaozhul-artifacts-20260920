//! 核心 LSM 逻辑的集成测试。
//!
//! 验收重点：
//! - 跨三层同键覆盖后删除：点查被墓碑遮蔽、范围扫描无重复；
//! - 墓碑只在“确认更老层不存在同键”时才丢弃；
//! - 合并在两个故障注入点中断并“重启”后，旧值不复活、扫描无重复、清单原子一致。

use lsm_kv::{Config, CrashPoint, Db};
use std::sync::Arc;
use tempfile::TempDir;

fn open(dir: &TempDir) -> Arc<Db> {
    let cfg = Config::new(dir.path())
        .memtable_entries(10_000)
        .background_flush(false);
    Db::open(cfg).unwrap()
}

/// 构造跨三层同键覆盖后删除：
/// seg1(最老): k=old, a=1
/// seg2:       k=mid, b=2
/// seg3(最新): k=<墓碑>, c=3
fn setup_three_layers(dir: &TempDir) -> Arc<Db> {
    let db = open(dir);

    db.put("k", "old").unwrap();
    db.put("a", "1").unwrap();
    db.flush().unwrap();

    db.put("k", "mid").unwrap();
    db.put("b", "2").unwrap();
    db.flush().unwrap();

    db.delete("k").unwrap();
    db.put("c", "3").unwrap();
    db.flush().unwrap();

    assert_eq!(db.snapshot().segments.len(), 3);
    db
}

#[test]
fn point_get_returns_newest_version_and_tombstone_shadows() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    assert_eq!(
        db.get("k").unwrap(),
        None,
        "tombstone must shadow old values"
    );
    assert_eq!(db.get("a").unwrap(), Some("1".into()));
    assert_eq!(db.get("b").unwrap(), Some("2".into()));
    assert_eq!(db.get("c").unwrap(), Some("3".into()));
}

#[test]
fn scan_has_no_duplicates_and_hides_deleted_keys() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    let items = db.scan(None, None).unwrap();
    assert_eq!(
        items,
        vec![
            ("a".to_string(), "1".to_string()),
            ("b".to_string(), "2".to_string()),
            ("c".to_string(), "3".to_string()),
        ],
        "k 跨三层各有一个版本，扫描结果中只能出现一次且因墓碑被遮蔽"
    );

    // 范围边界 [b, k)：含 b/c，不含 k
    let items = db.scan(Some("b"), Some("k")).unwrap();
    assert_eq!(
        items,
        vec![("b".into(), "2".into()), ("c".into(), "3".into())]
    );
}

#[test]
fn tombstone_kept_while_older_layer_may_contain_same_key() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    // 只合并最新两层 (seg3, seg2)，更老层 seg1 仍含 k=old。
    // 墓碑 k 必须保留 -> dropped_tombstones 为空。
    let report = db.compact_range(0, 1, None).unwrap();
    assert!(
        report.dropped_tombstones.is_empty(),
        "更老层存在同键时绝不能丢墓碑: {:?}",
        report.dropped_tombstones
    );
    assert_eq!(db.snapshot().segments.len(), 2);

    assert_eq!(db.get("k").unwrap(), None, "合并后旧值仍不得复活");
    let items = db.scan(None, None).unwrap();
    assert_eq!(items.len(), 3, "a/b/c 三个键，k 仍被墓碑遮蔽");
    assert!(items.iter().all(|(k, _)| k != "k"));
}

#[test]
fn tombstone_dropped_only_after_confirming_no_older_copy() {
    let dir = TempDir::new().unwrap();
    let db = open(&dir);

    // 最老层不含 k；最新层是 k 的墓碑。
    db.put("z", "z1").unwrap();
    db.flush().unwrap(); // seg1（更老层，无 k）

    db.put("k", "v").unwrap();
    db.flush().unwrap(); // seg2

    db.delete("k").unwrap();
    db.flush().unwrap(); // seg3：墓碑

    // 合并最新两层，older(seg1) 中确认无 k -> 可安全丢弃墓碑。
    let report = db.compact_range(0, 1, None).unwrap();
    assert_eq!(report.dropped_tombstones, vec!["k".to_string()]);
    assert_eq!(db.get("k").unwrap(), None);

    // 全量合并时不存在更老层，墓碑全部可丢。
    let report = db.compact_full(None).unwrap();
    assert!(report.dropped_tombstones.is_empty());
    assert_eq!(db.get("k").unwrap(), None);
}

#[test]
fn full_compaction_drops_tombstones_and_survives_restart() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    let report = db.compact_full(None).unwrap();
    assert_eq!(
        report.dropped_tombstones,
        vec!["k".to_string()],
        "全量合并无更老层，墓碑可安全丢弃"
    );
    assert_eq!(db.snapshot().segments.len(), 1);

    // 模拟重启：重新打开数据目录。
    drop(db);
    let db2 = open(&dir);
    assert_eq!(db2.snapshot().segments.len(), 1);
    assert_eq!(db2.get("k").unwrap(), None, "重启后旧值不复活");
    assert_eq!(
        db2.scan(None, None).unwrap(),
        vec![
            ("a".into(), "1".into()),
            ("b".into(), "2".into()),
            ("c".into(), "3".into()),
        ]
    );
}

#[test]
fn crash_after_new_segment_before_manifest_then_restart() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    // 故障点 1：新段已落盘，MANIFEST 未切换。
    let err = db
        .compact_full(Some(CrashPoint::AfterNewSegmentBeforeManifest))
        .unwrap_err();
    assert!(format!("{err:#}").contains("crash injection"));

    // 新段文件已存在（孤儿），但 MANIFEST 仍原子地指向旧三段。
    let orphan = dir.path().join("segments").join("4.seg");
    assert!(orphan.exists(), "新段应已 fsync+rename 落盘");
    assert_eq!(db.snapshot().segments.len(), 3, "内存态未切换清单");
    assert_eq!(db.get("k").unwrap(), None);

    // “重启”：open 必须发现孤儿段并清理，状态回到崩溃前一致快照。
    drop(db);
    let db2 = open(&dir);
    assert!(!orphan.exists(), "重启后孤儿段必须被清理");
    assert_eq!(db2.snapshot().segments.len(), 3);
    assert_eq!(db2.get("k").unwrap(), None, "旧值不得复活");
    assert_eq!(
        db2.scan(None, None).unwrap(),
        vec![
            ("a".into(), "1".into()),
            ("b".into(), "2".into()),
            ("c".into(), "3".into()),
        ],
        "范围扫描无重复、无已删除键"
    );

    // 恢复后应能正常完成合并（段 id 可复用，不影响正确性）。
    let report = db2.compact_full(None).unwrap();
    assert_eq!(report.new_segment_id, Some(4));
    assert_eq!(db2.get("k").unwrap(), None);

    drop(db2);
    let db3 = open(&dir);
    assert_eq!(db3.snapshot().segments.len(), 1);
    assert_eq!(db3.get("k").unwrap(), None);
    assert_eq!(db3.scan(None, None).unwrap().len(), 3);
}

#[test]
fn crash_after_manifest_switch_before_old_file_delete_then_restart() {
    let dir = TempDir::new().unwrap();
    let db = setup_three_layers(&dir);

    // 故障点 2：MANIFEST 已原子切换为新段，旧段文件尚未删除。
    let err = db
        .compact_full(Some(CrashPoint::AfterManifestBeforeDeleteOld))
        .unwrap_err();
    assert!(format!("{err:#}").contains("crash injection"));

    // 旧段文件仍残留在磁盘上。
    for id in 1..=3 {
        assert!(dir
            .path()
            .join("segments")
            .join(format!("{id}.seg"))
            .exists());
    }

    // 重启：以新 MANIFEST 为准，旧文件作为孤儿删除；墓碑已在新段中被合法丢弃。
    drop(db);
    let db2 = open(&dir);
    for id in 1..=3 {
        assert!(!dir
            .path()
            .join("segments")
            .join(format!("{id}.seg"))
            .exists());
    }
    assert_eq!(db2.snapshot().segments.len(), 1);
    assert_eq!(db2.get("k").unwrap(), None, "清单已切换，旧值不复活");
    assert_eq!(
        db2.scan(None, None).unwrap(),
        vec![
            ("a".into(), "1".into()),
            ("b".into(), "2".into()),
            ("c".into(), "3".into()),
        ]
    );
}

#[test]
fn multi_version_scan_without_delete_picks_newest_once() {
    let dir = TempDir::new().unwrap();
    let db = open(&dir);

    db.put("k", "v1").unwrap();
    db.flush().unwrap();
    db.put("k", "v2").unwrap();
    db.flush().unwrap();
    db.put("k", "v3").unwrap();
    db.flush().unwrap();
    db.put("k", "v4-mem").unwrap(); // 留在 memtable

    assert_eq!(db.get("k").unwrap(), Some("v4-mem".into()));
    assert_eq!(
        db.scan(None, None).unwrap(),
        vec![("k".into(), "v4-mem".into())],
        "四层中同键只出现一次"
    );
}

#[test]
fn delete_nonexistent_key_then_compaction_is_idempotent() {
    let dir = TempDir::new().unwrap();
    let db = open(&dir);
    db.put("x", "1").unwrap();
    db.flush().unwrap();
    db.delete("ghost").unwrap(); // 仅墓碑
    db.flush().unwrap();

    assert_eq!(db.get("ghost").unwrap(), None);
    let report = db.compact_full(None).unwrap();
    assert_eq!(report.dropped_tombstones, vec!["ghost".to_string()]);
    assert_eq!(db.get("ghost").unwrap(), None);
    assert_eq!(db.scan(None, None).unwrap(), vec![("x".into(), "1".into())]);
}
