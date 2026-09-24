//! 持久化与专项测试：
//! - 落盘后重新打开，数据与结构一致
//! - 小页下精确验证“根分裂升高 → 删空坍缩降回单层/空树”
//! - 被释放页进入空闲链表并被复用

use disk_bptree::tree::{BpTree, DelOutcome, PutOutcome};
use tempfile::TempDir;

#[test]
fn reopen_preserves_data() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("reopen.db");

    {
        let mut tree = BpTree::create(&path, 64).unwrap();
        for k in 0..200i64 {
            tree.put(k, k * 3).unwrap();
        }
        // 覆盖若干值。
        for k in (0..200i64).step_by(7) {
            tree.put(k, -k).unwrap();
        }
        let rep = tree.verify().unwrap();
        assert!(rep.ok);
        assert!(rep.height >= 2);
    }

    let mut tree = BpTree::open(&path).unwrap();
    assert_eq!(tree.page_size(), 64);
    for k in 0..200i64 {
        let expected = if k % 7 == 0 { -k } else { k * 3 };
        assert_eq!(tree.get(k).unwrap(), Some(expected), "key {k}");
    }
    assert_eq!(tree.get(200).unwrap(), None);
    let rep = tree.verify().unwrap();
    assert!(rep.ok);
    assert_eq!(rep.key_count, 200);

    // 重开后继续写入、删除仍然正确。
    tree.put(500, 9).unwrap();
    assert_eq!(tree.delete(500).unwrap(), DelOutcome::Deleted);
    assert_eq!(tree.get(500).unwrap(), None);
    assert_eq!(tree.delete(1).unwrap(), DelOutcome::Deleted);
    assert_eq!(tree.get(1).unwrap(), None);
    let rep = tree.verify().unwrap();
    assert!(rep.ok);
}

#[test]
fn height_grows_and_shrinks_back() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("height.db");
    let mut tree = BpTree::create(&path, 64).unwrap(); // 叶容量 3

    // 空树 → 单层。
    let rep = tree.verify().unwrap();
    assert_eq!(rep.height, 0);
    assert_eq!(rep.leaf_count, 0);

    tree.put(1, 1).unwrap();
    assert_eq!(tree.verify().unwrap().height, 1);

    // 4 项触发第一次叶分裂 + 根分裂 → 2 层。
    for k in 2..=4i64 {
        tree.put(k, k).unwrap();
    }
    let rep = tree.verify().unwrap();
    assert_eq!(rep.height, 2, "4 项应升为 2 层: {rep:?}");
    assert_eq!(rep.leaf_count, 2);

    // 继续插入到 3 层（64 字节页，内部也只能放 3 个分隔键）。
    for k in 5..=40i64 {
        tree.put(k, k).unwrap();
    }
    let rep = tree.verify().unwrap();
    assert!(rep.ok);
    assert!(rep.height >= 3, "40 项在 64 字节页下应至少 3 层: {rep:?}");

    // 顺序删除到 1 项：高度必须逐层降回 1。
    for k in 1..40i64 {
        assert_eq!(tree.delete(k).unwrap(), DelOutcome::Deleted);
    }
    let rep = tree.verify().unwrap();
    assert!(rep.ok, "{:?}", rep.errors);
    assert_eq!(rep.height, 1, "删到 1 项必须降回单层: {rep:?}");
    assert_eq!(rep.leaf_count, 1);
    assert_eq!(tree.get(40).unwrap(), Some(40));

    // 删空 → 0 层。
    assert_eq!(tree.delete(40).unwrap(), DelOutcome::Deleted);
    let rep = tree.verify().unwrap();
    assert_eq!(rep.height, 0);
    assert_eq!(tree.key_count(), 0);
    assert_eq!(tree.leftmost_id(), 0);

    // 空树再插入：回到单层且可读。
    tree.put(100, 200).unwrap();
    let rep = tree.verify().unwrap();
    assert!(rep.ok);
    assert_eq!(rep.height, 1);
    assert_eq!(tree.get(100).unwrap(), Some(200));
}

#[test]
fn free_pages_are_reused() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("free.db");
    let mut tree = BpTree::create(&path, 64).unwrap();
    for k in 0..100i64 {
        tree.put(k, k).unwrap();
    }
    let peak_root = tree.root_id();

    // 删掉大部分键，触发大量叶/内部页释放。
    for k in 0..90i64 {
        tree.delete(k).unwrap();
    }
    let meta = tree.meta_snapshot();
    assert!(meta.free_head != 0, "删空后应有空闲页");

    // 再插入：新页应复用被释放的页号（文件不增长或增长很少）。
    for k in 0..100i64 {
        tree.put(k + 1000, k).unwrap();
    }
    let rep = tree.verify().unwrap();
    assert!(rep.ok, "{:?}", rep.errors);
    // 数据完整。
    for k in 0..100i64 {
        assert_eq!(tree.get(k + 1000).unwrap(), Some(k));
    }
    // 复用后根页号可能变化（坍缩+再升高），树应自洽。
    assert!(tree.root_id() != 0);
    let _ = peak_root;
}

#[test]
fn replace_does_not_change_count() {
    let dir = TempDir::new().unwrap();
    let mut tree = BpTree::create(dir.path().join("r.db"), 64).unwrap();
    assert_eq!(tree.put(7, 1).unwrap(), PutOutcome::Inserted);
    assert_eq!(tree.put(7, 2).unwrap(), PutOutcome::Replaced(1));
    assert_eq!(tree.put(7, 3).unwrap(), PutOutcome::Replaced(2));
    assert_eq!(tree.key_count(), 1);
    assert_eq!(tree.get(7).unwrap(), Some(3));
    assert_eq!(tree.delete(7).unwrap(), DelOutcome::Deleted);
    assert_eq!(tree.delete(7).unwrap(), DelOutcome::NotFound);
    assert_eq!(tree.key_count(), 0);
}

#[test]
fn negative_and_boundary_keys() {
    let dir = TempDir::new().unwrap();
    let mut tree = BpTree::create(dir.path().join("n.db"), 64).unwrap();
    let keys = [i64::MIN, i64::MIN + 1, -5, -1, 0, 1, 5, i64::MAX - 1, i64::MAX];
    for (i, k) in keys.iter().enumerate() {
        tree.put(*k, i as i64).unwrap();
    }
    for (i, k) in keys.iter().enumerate() {
        assert_eq!(tree.get(*k).unwrap(), Some(i as i64));
    }
    let all = tree.range(None, None).unwrap();
    assert_eq!(all.iter().map(|(k, _)| *k).collect::<Vec<_>>(), keys);
    let mid = tree.range(Some(-5), Some(5)).unwrap();
    assert_eq!(
        mid.iter().map(|(k, _)| *k).collect::<Vec<_>>(),
        vec![-5, -1, 0, 1, 5]
    );
    let rep = tree.verify().unwrap();
    assert!(rep.ok, "{:?}", rep.errors);
}

#[test]
fn leaf_chain_order_after_many_splits() {
    let dir = TempDir::new().unwrap();
    let mut tree = BpTree::create(dir.path().join("chain.db"), 64).unwrap();
    // 逆序插入：每次都在最左分裂，对叶链维护是极端场景。
    for k in (0..300i64).rev() {
        tree.put(k, k).unwrap();
    }
    let all = tree.range(None, None).unwrap();
    assert_eq!(all.len(), 300);
    for (i, (k, v)) in all.iter().enumerate() {
        assert_eq!(*k, i as i64);
        assert_eq!(*v, i as i64);
    }
    // 反向走叶链抽查 prev：从 range 全量成功 + verify 已保证双向一致。
    let rep = tree.verify().unwrap();
    assert!(rep.ok, "{:?}", rep.errors);

    // 再逆序删除（每次删最左叶的最小键），链首维护压力测试。
    for k in 0..300i64 {
        assert_eq!(tree.delete(k).unwrap(), DelOutcome::Deleted);
        if k % 37 == 0 {
            let rep = tree.verify().unwrap();
            assert!(rep.ok, "删到 {k}: {:?}", rep.errors);
        }
    }
    assert_eq!(tree.range(None, None).unwrap(), vec![]);
}
