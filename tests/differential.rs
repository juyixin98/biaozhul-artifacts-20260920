//! 验收差分测试：固定种子随机操作，对照内存有序映射（BTreeMap），
//! 每一步都校验：
//! 1. 全树结构（分隔键路由正确性、各节点最低/最高占用率、分隔键升序）——`BPTree::verify`；
//! 2. 叶链：最左叶沿 next 的顺序必须与中序遍历完全一致，且相邻叶键严格衔接；
//! 3. 数据内容：`range(MIN, MAX)` 必须与 BTreeMap 完全一致，抽样点查一致。
//!
//! 最后删除全部键，确认树高从多层重新降为 1。
//!
//! 不引入额外依赖：用确定性 xorshift64 作为固定随机源。

use bptree_index::bptree::BPTree;
use bptree_index::pager::{FilePager, MemoryPager, Pager};
use std::collections::BTreeMap;

/// 确定性伪随机数（xorshift64*），种子写死以保证可复现。
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }
    fn i64_in(&mut self, lo: i64, span: u64) -> i64 {
        lo + (self.below(span) as i64)
    }
}

fn reference_pairs(reference: &BTreeMap<i64, u64>) -> Vec<(i64, u64)> {
    reference.iter().map(|(k, v)| (*k, *v)).collect()
}

/// 对多种很小的页大小各跑一轮固定随机工作负载。
#[test]
fn differential_against_btreemap_tiny_pages() {
    for &page_size in &[64usize, 80, 128, 256] {
        run_one(page_size, 0x9E37_79B9_7F4A_7C15 ^ page_size as u64);
    }
}

fn run_one(page_size: usize, seed: u64) {
    let mut rng = Rng(seed);
    let mut tree = BPTree::new(MemoryPager::new(page_size));
    let mut reference: BTreeMap<i64, u64> = BTreeMap::new();

    const KEY_LO: i64 = -200;
    const KEY_SPAN: u64 = 401; // 键域 [-200, 200]
    const OPS: u64 = 12_000;

    let mut max_height = 1usize;

    for step in 0..OPS {
        let key = rng.i64_in(KEY_LO, KEY_SPAN);
        let roll = rng.below(100);
        if roll < 52 {
            // 插入或覆盖
            let value = rng.next_u64();
            let inserted = tree.insert(key, value);
            let existed = reference.insert(key, value).is_some();
            assert_eq!(
                inserted, !existed,
                "步 {step}: insert 返回标志不一致 key={key}"
            );
        } else if roll < 84 {
            // 删除
            let found = tree.delete(key);
            let expected = reference.remove(&key).is_some();
            assert_eq!(
                found, expected,
                "步 {step}: delete 返回标志不一致 key={key}"
            );
        } else if roll < 92 {
            // 点查
            let got = tree.get(key);
            let want = reference.get(&key).copied();
            assert_eq!(got, want, "步 {step}: get 不一致 key={key}");
        } else {
            // 随机范围读
            let mut a = rng.i64_in(KEY_LO, KEY_SPAN);
            let mut b = rng.i64_in(KEY_LO, KEY_SPAN);
            if a > b {
                std::mem::swap(&mut a, &mut b);
            }
            let got = tree.range(a, b);
            let want: Vec<(i64, u64)> = reference.range(a..=b).map(|(k, v)| (*k, *v)).collect();
            assert_eq!(got, want, "步 {step}: range({a},{b}) 不一致");
        }

        // —— 每步核心校验：分隔键 / 占用率 / 叶链 ——
        let stats = tree
            .verify()
            .unwrap_or_else(|e| panic!("步 {step}: 树结构校验失败（页大小 {page_size}）：{e}"));
        max_height = max_height.max(stats.height);

        // 全量数据对照（沿叶链顺序读出）
        let all = tree.range(i64::MIN, i64::MAX);
        assert_eq!(
            all,
            reference_pairs(&reference),
            "步 {step}: 全量内容与内存映射不一致（页大小 {page_size}）"
        );
        // 占用率数值合理
        for o in &stats.occupancies {
            assert!(*o >= 0.0 && *o <= 1.0 + 1e-9, "步 {step}: 非法占用率 {o}");
        }
        assert_eq!(
            stats.item_count,
            reference.len(),
            "步 {step}: item_count 不符"
        );
    }

    // 工作负载期间树必须确实长高过。
    assert!(
        max_height >= 2,
        "页大小 {page_size}: 工作负载期间树高未增加（max={max_height}），未覆盖根分裂"
    );

    // —— 逐步删除全部键，覆盖借位/合并/根塌缩 ——
    let keys: Vec<i64> = reference.keys().copied().collect();
    for (removed, key) in keys.iter().enumerate() {
        assert!(tree.delete(*key), "收尾删除 {key} 应存在");
        reference.remove(key);
        let stats = tree
            .verify()
            .unwrap_or_else(|e| panic!("收尾删除阶段（页 {page_size}）：{e}"));
        assert_eq!(
            stats.item_count,
            reference.len(),
            "收尾删除第 {removed} 步计数不一致"
        );
        assert_eq!(
            tree.range(i64::MIN, i64::MAX),
            reference_pairs(&reference),
            "收尾删除第 {removed} 步内容不一致"
        );
    }

    let final_stats = tree.verify().expect("清空后校验失败");
    assert_eq!(
        final_stats.height, 1,
        "页大小 {page_size}: 清空后树高应降回 1，实际 {}",
        final_stats.height
    );
    assert_eq!(final_stats.internal_pages, 0, "清空后不应残留内部页");
    assert_eq!(final_stats.leaf_pages, 1, "清空后应只剩单个根叶页");
    assert_eq!(final_stats.item_count, 0);
    assert!(tree.range(i64::MIN, i64::MAX).is_empty());
}

/// 偏删除的负载：先填满，再以高删除比例随机操作，重点压测借位与合并。
#[test]
fn differential_delete_heavy() {
    let page_size = 72usize; // 叶容量 3，内部容量 4，极易欠占用
    let mut rng = Rng(0xDEAD_BEEF_CAFE_BABE);
    let mut tree = BPTree::new(MemoryPager::new(page_size));
    let mut reference: BTreeMap<i64, u64> = BTreeMap::new();

    for k in -150..=150 {
        tree.insert(k, k as u64);
        reference.insert(k, k as u64);
    }
    tree.verify().unwrap();
    assert!(tree.verify().unwrap().height >= 3);

    for step in 0..20_000 {
        let key = rng.i64_in(-150, 301);
        if rng.below(100) < 35 {
            let v = rng.next_u64();
            tree.insert(key, v);
            reference.insert(key, v);
        } else {
            tree.delete(key);
            reference.remove(&key);
        }
        if step % 64 == 0 {
            tree.verify()
                .unwrap_or_else(|e| panic!("删除密集负载步 {step}: {e}"));
            assert_eq!(
                tree.range(i64::MIN, i64::MAX),
                reference_pairs(&reference),
                "删除密集负载步 {step} 内容不一致"
            );
        }
    }
    // 最终再清空，确保降回单层。
    for k in reference.keys().copied().collect::<Vec<_>>() {
        tree.delete(k);
    }
    let st = tree.verify().unwrap();
    assert_eq!(st.height, 1);
    assert_eq!(st.item_count, 0);
}

/// 磁盘持久化：写入 -> close -> 重开，数据与校验结果必须保持一致。
#[test]
fn file_persistence_across_reopen() {
    let dir = std::env::temp_dir();
    let path = dir.join(format!(
        "bptree_diff_{}_{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));

    let pairs: Vec<(i64, u64)> = {
        let pager = FilePager::create(&path, 96).expect("create");
        let mut tree = BPTree::new(pager);
        let mut rng = Rng(0x1234_5678);
        for k in -250..=250 {
            let v = if rng.below(3) == 0 {
                rng.next_u64()
            } else {
                k as u64
            };
            // 跳过部分键，制造非满树
            if rng.below(100) < 80 {
                tree.insert(k, v);
            }
        }
        // 再随机删除一部分，使文件停留在多层状态
        for k in -250..=250 {
            if rng.below(100) < 25 {
                tree.delete(k);
            }
        }
        tree.verify().unwrap();
        let pairs = tree.range(i64::MIN, i64::MAX);
        tree.sync().unwrap();
        drop(tree);
        pairs
    };

    let pager = FilePager::open(&path).expect("reopen");
    let page_size = pager.page_size();
    assert_eq!(page_size, 96);
    let tree = BPTree::new(pager);
    tree.verify().expect("重开后校验失败");
    assert_eq!(tree.range(i64::MIN, i64::MAX), pairs);
    // 重开后仍可继续插入/删除
    let mut tree = tree;
    tree.insert(1_000_000, 42);
    assert_eq!(tree.get(1_000_000), Some(42));
    tree.delete(1_000_000);
    tree.verify().unwrap();

    drop(tree);
    let _ = std::fs::remove_file(&path);
}
