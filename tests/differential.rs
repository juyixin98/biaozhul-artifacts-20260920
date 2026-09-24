//! 固定种子随机操作差分测试：磁盘 B+ 树 vs 内存 `BTreeMap`。
//!
//! 每一步操作后都做三件事：
//! 1. 全树结构校验（分隔键不变量、占用率半满下限、叶链双向一致）
//! 2. 对模型映射中的每个键做点查对照，并抽查不存在的键
//! 3. 对照若干范围查询（全范围、中段、窄区间、无界）
//!
//! 覆盖要求：64 字节小页下，插入会让树高升到 3 层以上，
//! 随后删除到空会逐层坍缩回单层（根叶）再到空树。

use std::collections::BTreeMap;

use disk_bptree::tree::{BpTree, DelOutcome, PutOutcome};
use rand::rngs::StdRng;
use rand::{RngExt, SeedableRng};
use tempfile::TempDir;

fn open_tree(page_size: u16) -> (TempDir, std::path::PathBuf, BpTree) {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("test.db");
    // 关闭逐操作 fsync：本测试操作量大，崩溃恢复不在差分测试范围内。
    let tree = BpTree::create_opts(&path, page_size, false).unwrap();
    (dir, path, tree)
}

/// 固定种子、固定键空间的随机读写差分。
fn differential_cycle(seed: u64, page_size: u16, key_space: i64, rounds: usize) {
    let (_dir, _path, mut tree) = open_tree(page_size);
    let mut model: BTreeMap<i64, i64> = BTreeMap::new();
    let mut rng = StdRng::seed_from_u64(seed);

    let mut max_height = 0usize;
    let mut saw_merge = false;
    let mut prev_leaf_count = 1usize;

    for step in 0..rounds {
        // 偏置：前 1/3 以插入为主建立多层树；中间混合；后 1/3 删除更重，
        // 保证树高经历“升高 → 降回一层”的完整过程。
        let r: f64 = rng.random::<f64>();
        let insert_bias = if step < rounds / 3 {
            0.85
        } else if step > rounds * 2 / 3 {
            0.35
        } else {
            0.6
        };
        let key = rng.random_range(0..key_space);
        let value = rng.random_range(-1_000_000..1_000_000);

        if r < insert_bias {
            let outcome = tree.put(key, value).unwrap();
            match model.insert(key, value) {
                None => assert_eq!(outcome, PutOutcome::Inserted, "step {step}: key {key}"),
                Some(old) => assert_eq!(outcome, PutOutcome::Replaced(old), "step {step}"),
            }
        } else {
            let outcome = tree.delete(key).unwrap();
            match model.remove(&key) {
                None => assert_eq!(outcome, DelOutcome::NotFound, "step {step}"),
                Some(_) => assert_eq!(outcome, DelOutcome::Deleted, "step {step}"),
            }
        }

        // 每步：结构校验。
        let report = tree.verify().unwrap();
        assert!(report.ok, "step {step} seed {seed}: {:?}", report.errors);
        max_height = max_height.max(report.height);
        // 叶数减少意味着发生了叶合并（借位不改变叶数）。
        saw_merge |= report.leaf_count < prev_leaf_count;
        prev_leaf_count = report.leaf_count;
        assert_eq!(
            report.key_count as usize,
            model.len(),
            "step {seed}:{step} 键数不一致"
        );

        // 每步：全量点查对照（键空间小，全量扫得起）。
        for k in 0..key_space {
            assert_eq!(tree.get(k).unwrap(), model.get(&k).copied(), "get {k} @ {step}");
        }

        // 每步：范围读对照。
        check_range(&mut tree, &model, None, None, step);
        let a = rng.random_range(0..key_space);
        let b = rng.random_range(a..key_space);
        check_range(&mut tree, &model, Some(a), Some(b), step);
        let c = rng.random_range(0..key_space);
        check_range(&mut tree, &model, Some(c), None, step);
        check_range(&mut tree, &model, None, Some(c), step);
        // 反向区间必须返回空。
        if b > a {
            assert!(tree.range(Some(b), Some(a)).unwrap().is_empty());
        }
    }

    // 验收：树高确实升过（小页 + 足够多键），也确实降回过。
    assert!(max_height >= 3, "种子 {seed} 页 {page_size}：树高应升到 >=3，实际 {max_height}");

    // 清空：删到 0，覆盖“逐层坍缩回单层再到空树”。
    let keys: Vec<i64> = model.keys().copied().collect();
    let mut order = keys;
    // 洗牌删除，制造各种兄弟借位/合并方向。
    for i in (1..order.len()).rev() {
        let j = rng.random_range(0..=i);
        order.swap(i, j);
    }
    for (i, k) in order.iter().enumerate() {
        assert_eq!(tree.delete(*k).unwrap(), DelOutcome::Deleted);
        model.remove(k);
        let report = tree.verify().unwrap();
        assert!(report.ok, "清空阶段 {i}: {:?}", report.errors);
        assert_eq!(report.key_count as usize, model.len());
        if model.len() == 1 {
            assert_eq!(report.height, 1, "只剩一键时应为单层根叶");
            assert_eq!(report.leaf_count, 1);
        }
    }
    let report = tree.verify().unwrap();
    assert!(report.ok);
    assert_eq!(tree.key_count(), 0);
    assert_eq!(tree.root_id(), 0);
    assert_eq!(tree.leftmost_id(), 0);
    assert_eq!(tree.range(None, None).unwrap(), vec![]);

    // 空树再插入仍应正常（回到单层）。
    tree.put(42, 7).unwrap();
    assert_eq!(tree.get(42).unwrap(), Some(7));
    let rep = tree.verify().unwrap();
    assert!(rep.ok);
    assert_eq!(rep.height, 1);

    // 借位不改变叶数、难以从外部直接观察；合并在清空阶段必然大量发生。
    eprintln!(
        "种子 {seed} 页 {page_size}: 最大高度 {max_height}, 观察到叶合并={saw_merge}"
    );
}

fn check_range(
    tree: &mut BpTree,
    model: &BTreeMap<i64, i64>,
    start: Option<i64>,
    end: Option<i64>,
    step: usize,
) {
    let got = tree.range(start, end).unwrap();
    let expected: Vec<(i64, i64)> = model
        .range(start.unwrap_or(i64::MIN)..=end.unwrap_or(i64::MAX))
        .map(|(k, v)| (*k, *v))
        .collect();
    assert_eq!(got, expected, "range [{start:?},{end:?}] @ step {step}");
}

#[test]
fn differential_page64_seed1() {
    differential_cycle(1, 64, 120, 4000);
}

#[test]
fn differential_page64_seed2() {
    differential_cycle(2, 64, 200, 6000);
}

#[test]
fn differential_page67_seed3() {
    // 67 字节：内部 raw 容量为偶数，验证“下调为奇数”后整树仍正确。
    differential_cycle(3, 67, 150, 4000);
}

#[test]
fn differential_page128_seed4() {
    differential_cycle(4, 128, 600, 6000);
}

#[test]
fn differential_page256_seed5() {
    // 800 键 / 256 字节页：叶容量 15 → ~54 叶 → 内部 2 层，树高 3。
    differential_cycle(5, 256, 800, 4000);
}
