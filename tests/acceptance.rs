//! 验收级集成测试：
//!
//! 1. 全碰撞返回明确容量错误；
//! 2. 分裂中断恢复（提交后三个故障注入点 + 提交前窗口）；
//! 3. 目录共享桶引用正确；
//! 4. 与内存 HashMap 全量比对；
//! 5. 删除后的安全合并与目录收缩、合并中断恢复；
//! 6. 受控碰撞（mod:2）可以靠分裂分离；
//! 7. 关闭重开持久化；CRC 损坏被明确拒绝。

use std::collections::HashMap;

use ehindex::hash::HashKind;
use ehindex::{Fault, Index, IndexError};

fn temp_path(tag: &str) -> std::path::PathBuf {
    let mut dir = std::env::temp_dir();
    let pid = std::process::id();
    dir.push(format!("ehindex-test-{tag}-{pid}-{}.ehdb", nano()));
    dir
}

fn nano() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

/// 在注入故障点处期待一次“模拟崩溃”：调用返回 Err，随后 drop。
fn assert_crashed<T>(r: &Result<T, IndexError>) {
    match r {
        Err(IndexError::Corrupt(msg)) => assert!(msg.contains("故障注入"), "意外错误：{msg}"),
        Ok(_) => panic!("期望故障注入错误，但操作成功返回"),
        Err(other) => panic!("期望故障注入错误，得到 {other:?}"),
    }
}

/// mod:2 下按分裂位（bit31）收集两侧键。
fn collect_mod2_keys(want_zero: usize, want_one: usize) -> (Vec<String>, Vec<String>) {
    let h = ehindex::hash::hasher_for(HashKind::LowMod(2));
    let mut zeros = Vec::new();
    let mut ones = Vec::new();
    for k in 0..100000u32 {
        let key = format!("key-{k}");
        if h.hash(key.as_bytes()) & 0x8000_0000 == 0 && zeros.len() < want_zero {
            zeros.push(key);
        } else if h.hash(key.as_bytes()) & 0x8000_0000 != 0 && ones.len() < want_one {
            ones.push(key);
        }
        if zeros.len() == want_zero && ones.len() == want_one {
            break;
        }
    }
    assert_eq!(zeros.len(), want_zero, "fx mod2 应能找到足够 bit31=0 的键");
    assert_eq!(ones.len(), want_one, "fx mod2 应能找到足够 bit31=1 的键");
    (zeros, ones)
}

/// 用参考映射比对磁盘索引：逐键 GET + 全量 snapshot。
fn assert_matches_map(idx: &mut Index, map: &HashMap<Vec<u8>, Vec<u8>>) {
    // 逐键查询（含所有存在的键）
    for (k, v) in map {
        let got = idx.get(k).expect("get 不应失败");
        assert_eq!(got.as_deref(), Some(v.as_slice()), "键 {:?} 的值不符", k);
    }
    // 不存在键的抽样
    for k in ["nope", "missing", "___"] {
        assert_eq!(idx.get(k.as_bytes()).unwrap(), None);
    }
    // 全量快照与映射完全一致
    let snap: HashMap<Vec<u8>, Vec<u8>> = idx.snapshot().unwrap().into_iter().collect();
    assert_eq!(snap.len(), map.len(), "快照记录数不符");
    for (k, v) in map {
        assert_eq!(snap.get(k), Some(v), "快照中键 {:?} 不符", k);
    }
    let stats = idx.stats().unwrap();
    assert_eq!(stats.total_records, map.len());
    assert!(!stats.pending_journal, "正常状态不应有活动日志");
}

// --------------------------------------------------------------- 1. 全碰撞

#[test]
fn full_collision_returns_explicit_capacity_error() {
    let path = temp_path("collision");
    let mut idx = Index::create(&path, 2, HashKind::Const(0)).unwrap();
    idx.put(b"a", b"1").unwrap();
    idx.put(b"b", b"2").unwrap();
    // 桶满；第 3 个键散列与桶内完全相同 -> 立刻明确报错，不做任何分裂。
    let err = idx.put(b"c", b"3").unwrap_err();
    match err {
        IndexError::BucketCapacityExhausted { bucket, reason } => {
            assert_eq!(bucket, 0, "全碰撞下目录从未分裂，仍引用 0 号桶");
            assert!(reason.contains("散列完全相同"), "原因应为全碰撞：{reason}");
        }
        other => panic!("期望 BucketCapacityExhausted，得到 {other:?}"),
    }
    // 索引仍可用：已插入数据完好。
    assert_eq!(idx.get(b"a").unwrap().as_deref(), Some(b"1".as_slice()));
    assert_eq!(idx.global_depth(), 0, "全碰撞不得导致目录倍增");
    let stats = idx.stats().unwrap();
    assert_eq!(stats.total_records, 2);
    assert_eq!(stats.live_buckets, 1);

    // 重开：无日志、状态完好。
    drop(idx);
    let mut reopened = Index::open(&path).unwrap();
    assert_eq!(reopened.global_depth(), 0);
    assert_eq!(
        reopened.get(b"b").unwrap().as_deref(),
        Some(b"2".as_slice())
    );
}

#[test]
fn const_hasher_with_nontrivial_value_still_full_collision() {
    // const:12345 —— 不管常数是什么，全部相同。
    let path = temp_path("collision-const-v");
    let mut idx = Index::create(&path, 3, HashKind::Const(12345)).unwrap();
    for k in 0..3u8 {
        idx.put(&[k], &[k]).unwrap();
    }
    assert!(matches!(
        idx.put(b"z", b"zz").unwrap_err(),
        IndexError::BucketCapacityExhausted { .. }
    ));
}

// --------------------------------------------------- 2. 受控碰撞可被分裂分离

#[test]
fn controlled_collision_mod2_splits_apart_fixed() {
    let path = temp_path("mod2-fixed");
    let mut idx = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    // 直接选取 bit31 两侧、且第 2 分裂位也能分开的键，验证首次分裂即成功。
    let h = ehindex::hash::hasher_for(HashKind::LowMod(2));
    let pick = |want_hi: bool, used: &[String], n: usize| -> Vec<String> {
        let mut out = Vec::new();
        for k in 0..200000u32 {
            let key = format!("key-{k}");
            if used.iter().any(|u| u == &key) || out.iter().any(|o| o == &key) {
                continue;
            }
            let v = h.hash(key.as_bytes());
            if (v & 0x8000_0000 != 0) == want_hi {
                out.push(key);
            }
            if out.len() == n {
                break;
            }
        }
        out
    };
    // 为保证第一次分裂后两侧各自容纳，只插入 2 个键即可，第三个用另一侧键触发。
    let zeros = pick(false, &[], 2);
    let ones = pick(true, &zeros, 2);
    assert_eq!(zeros.len(), 2);
    assert_eq!(ones.len(), 2);

    idx.put(zeros[0].as_bytes(), b"z0").unwrap();
    idx.put(zeros[1].as_bytes(), b"z1").unwrap();
    assert_eq!(idx.global_depth(), 0);

    // 满桶后插入高位键：分裂链原子完成，新键进入新桶。
    idx.put(ones[0].as_bytes(), b"o0").unwrap();
    assert_eq!(idx.global_depth(), 1, "首次分裂倍增到 gd=1");
    let dirs: std::collections::HashSet<u32> = idx.directory().iter().copied().collect();
    assert_eq!(dirs.len(), 2, "gd=1 下两个槽指向两个不同桶");

    // 再插一个高位键（其同桶现在有 1 条，能容纳）。
    idx.put(ones[1].as_bytes(), b"o1").unwrap();

    let mut map = HashMap::new();
    map.insert(zeros[0].as_bytes().to_vec(), b"z0".to_vec());
    map.insert(zeros[1].as_bytes().to_vec(), b"z1".to_vec());
    map.insert(ones[0].as_bytes().to_vec(), b"o0".to_vec());
    map.insert(ones[1].as_bytes().to_vec(), b"o1".to_vec());
    assert_matches_map(&mut idx, &map);
}

/// `mod:2` 的设计意图：同余数类的键在顶端位长期聚集，插入第 3 个同类键时
/// 需要“空转分裂”，但因为低位保留熵，最终仍能分开（而非全碰撞报错）。
#[test]
fn controlled_collision_mod2_same_class_splits_with_deeper_chain() {
    let path = temp_path("mod2-chain");
    let mut idx = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    let h = ehindex::hash::hasher_for(HashKind::LowMod(2));
    // 3 个 bit31=0 但整体散列互异的键。
    let mut zeros = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for k in 0..200000u32 {
        let key = format!("c-{k}");
        let v = h.hash(key.as_bytes());
        if v & 0x8000_0000 == 0 && seen.insert(v) {
            zeros.push(key);
        }
        if zeros.len() == 3 {
            break;
        }
    }
    assert_eq!(zeros.len(), 3);
    idx.put(zeros[0].as_bytes(), b"a").unwrap();
    idx.put(zeros[1].as_bytes(), b"b").unwrap();
    // 第三个同类键：不报错（区别于 const 全碰撞），分裂链把它们分开。
    idx.put(zeros[2].as_bytes(), b"c").unwrap();
    assert!(idx.global_depth() >= 1);
    assert_eq!(
        idx.get(zeros[2].as_bytes()).unwrap().as_deref(),
        Some(b"c".as_slice())
    );
    assert_eq!(
        idx.get(zeros[0].as_bytes()).unwrap().as_deref(),
        Some(b"a".as_slice())
    );
}

// ------------------------------------------------------------- 3. 分裂恢复

/// 在指定故障点制造分裂崩溃，重开后索引必须自洽且数据不丢。
fn split_recovery_at(fault: Fault, tag: &str) {
    let path = temp_path(tag);
    let mut idx = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    let (zeros, ones) = collect_mod2_keys(2, 1);
    idx.put(zeros[0].as_bytes(), b"z0").unwrap();
    idx.put(zeros[1].as_bytes(), b"z1").unwrap();

    // 注入故障后，触发满桶分裂。
    idx.inject_fault(fault);
    let r = idx.put(ones[0].as_bytes(), b"o0");
    assert_crashed(&r);
    drop(idx);

    // 重开走恢复。
    let mut idx = Index::open(&path).unwrap();
    // 无论崩溃发生在哪个点：
    // - 已提交的 z0/z1 必然可读；
    // - 分裂要么已完成（ones[0] 可读），要么整个事务未生效（ones[0] 不存在），
    //   两种结局下结构都必须自洽（assert_invariants 在 open 内执行）。
    assert_eq!(
        idx.get(zeros[0].as_bytes()).unwrap().as_deref(),
        Some(b"z0".as_slice())
    );
    assert_eq!(
        idx.get(zeros[1].as_bytes()).unwrap().as_deref(),
        Some(b"z1".as_slice())
    );
    let split_committed = idx.global_depth() == 1;
    if split_committed {
        assert_eq!(
            idx.get(ones[0].as_bytes()).unwrap().as_deref(),
            Some(b"o0".as_slice())
        );
    } else {
        assert_eq!(idx.global_depth(), 0);
        assert_eq!(idx.get(ones[0].as_bytes()).unwrap(), None);
        // 事务未生效时，可以重新发起同样的分裂并成功。
        idx.put(ones[0].as_bytes(), b"o0").unwrap();
        assert_eq!(idx.global_depth(), 1);
        assert_eq!(
            idx.get(ones[0].as_bytes()).unwrap().as_deref(),
            Some(b"o0".as_slice())
        );
    }
    let stats = idx.stats().unwrap();
    assert_eq!(stats.hole_pages, 0, "洞页应在打开时回收");
}

#[test]
fn split_recovery_before_redirect() {
    split_recovery_at(Fault::SplitBeforeDirRedirect, "split-before");
}

#[test]
fn split_recovery_during_redirect() {
    split_recovery_at(Fault::SplitDuringDirRedirect, "split-during");
}

#[test]
fn split_recovery_before_journal_clear() {
    split_recovery_at(Fault::SplitBeforeJournalClear, "split-clear");
}

#[test]
fn split_recovery_is_idempotent_on_second_open() {
    // 在“日志未清”状态下连续重开两次，重放必须幂等。
    let path = temp_path("split-twice");
    let mut idx = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    let (zeros, ones) = collect_mod2_keys(2, 1);
    idx.put(zeros[0].as_bytes(), b"z0").unwrap();
    idx.put(zeros[1].as_bytes(), b"z1").unwrap();
    idx.inject_fault(Fault::SplitBeforeJournalClear);
    assert_crashed(&idx.put(ones[0].as_bytes(), b"o0"));
    drop(idx);

    let first = Index::open(&path).unwrap();
    drop(first);
    let mut second = Index::open(&path).unwrap();
    assert_eq!(
        second.get(ones[0].as_bytes()).unwrap().as_deref(),
        Some(b"o0".as_slice())
    );
}

#[test]
fn split_recovery_before_commit_loses_no_committed_data() {
    // 提交点之前崩溃：目录可能已倍增，但桶页未动、无日志。
    // 重开后已提交数据必须完好；新键视作未提交（不存在），可重新插入。
    let path = temp_path("split-precommit");
    let mut idx = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    let (zeros, ones) = collect_mod2_keys(2, 1);
    idx.put(zeros[0].as_bytes(), b"z0").unwrap();
    idx.put(zeros[1].as_bytes(), b"z1").unwrap();
    idx.inject_fault(Fault::SplitBeforeCommit);
    assert_crashed(&idx.put(ones[0].as_bytes(), b"o0"));
    drop(idx);

    let mut idx = Index::open(&path).unwrap();
    // 已提交两键完好。
    assert_eq!(
        idx.get(zeros[0].as_bytes()).unwrap().as_deref(),
        Some(b"z0".as_slice())
    );
    assert_eq!(
        idx.get(zeros[1].as_bytes()).unwrap().as_deref(),
        Some(b"z1".as_slice())
    );
    // 待插入键未提交：不存在。
    assert_eq!(idx.get(ones[0].as_bytes()).unwrap(), None);
    // 无活动日志；孤儿洞页已回收（若有）。
    assert!(!idx.stats().unwrap().pending_journal);
    assert_eq!(idx.stats().unwrap().hole_pages, 0);
    // 可以重新发起插入并成功。
    idx.put(ones[0].as_bytes(), b"o0").unwrap();
    assert_eq!(
        idx.get(ones[0].as_bytes()).unwrap().as_deref(),
        Some(b"o0".as_slice())
    );
}

// ------------------------------------------------- 4. 目录共享桶引用正确性

#[test]
fn directory_shared_bucket_references_are_correct() {
    // cap 较大，制造 gd 增长但部分桶尚未分裂的情形。
    let path = temp_path("sharing");
    let mut idx = Index::create(&path, 6, HashKind::Fx).unwrap();

    // 顺序插入直到出现第一次分裂：目录变 2 槽，一个桶被两个槽共享。
    let mut n = 0u32;
    while idx.global_depth() == 0 {
        idx.put(format!("shared-{n}").as_bytes(), format!("v{n}").as_bytes())
            .unwrap();
        n += 1;
        assert!(n < 100000, "fx 散列下应很快出现分裂");
    }
    assert_eq!(idx.global_depth(), 1);
    let dir = idx.directory().to_vec();
    assert_ne!(dir[0], dir[1], "分裂后两个槽应指向不同桶");

    // 继续插入到 gd=2，期间必然存在“未被再次分裂的桶”被多个槽共享。
    let mut max_share = 1usize;
    while idx.global_depth() < 3 {
        idx.put(format!("shared-{n}").as_bytes(), format!("v{n}").as_bytes())
            .unwrap();
        n += 1;
        let mut counts: HashMap<u32, usize> = HashMap::new();
        for &b in idx.directory() {
            *counts.entry(b).or_insert(0) += 1;
        }
        max_share = max_share.max(*counts.values().max().unwrap());
        assert!(n < 500000);
    }
    assert!(max_share >= 2, "gd 增长过程中应出现桶被多槽共享");

    // 不变量（assert_invariants 私有，在每次 put/open 内部执行；
    // 这里通过统计与逐桶核对公开验证引用块）。
    let gd = idx.global_depth() as usize;
    let mut by_bucket: HashMap<u32, Vec<usize>> = HashMap::new();
    for (slot, &b) in idx.directory().iter().enumerate() {
        by_bucket.entry(b).or_default().push(slot);
    }
    for (bid, slots) in &by_bucket {
        let (ld, count) = idx.bucket_debug(*bid).unwrap();
        let expected = 1usize << (gd - ld as usize);
        assert_eq!(slots.len(), expected, "桶 {bid} 被引用槽数错误");
        assert_eq!(slots[0] % expected, 0, "桶 {bid} 引用块未对齐");
        assert!(count <= 6);
    }
}

// ------------------------------------------------- 5. 合并、收缩与合并恢复

#[test]
fn merge_and_directory_shrink_back_to_zero() {
    let path = temp_path("merge-shrink");
    // 用 Fx 哈希、cap=2，构造“恰好一次分裂、gd=1、两个叶子桶各 2 条”的状态。
    let mut idx = Index::create(&path, 2, HashKind::Fx).unwrap();
    let h = ehindex::hash::hasher_for(HashKind::Fx);
    // 找 2 个 bit31=0 键和 2 个 bit31=1 键，且同类内第 2 位也不同（避免空转）。
    let mut lo = Vec::new();
    let mut hi = Vec::new();
    for k in 0..500000u32 {
        let key = format!("m-{k}");
        let v = h.hash(key.as_bytes());
        let bucket = if v & 0x8000_0000 == 0 {
            &mut lo
        } else {
            &mut hi
        };
        if bucket.len() < 2 && !bucket.iter().any(|(vv, _)| *vv == v) {
            bucket.push((v, key));
        }
        if lo.len() == 2 && hi.len() == 2 {
            break;
        }
    }
    assert_eq!(lo.len(), 2);
    assert_eq!(hi.len(), 2);
    let lo_keys: Vec<String> = lo.iter().map(|(_, k)| k.clone()).collect();
    let hi_keys: Vec<String> = hi.iter().map(|(_, k)| k.clone()).collect();

    for (i, k) in lo_keys.iter().enumerate() {
        idx.put(k.as_bytes(), format!("l{i}").as_bytes()).unwrap();
    }
    for (i, k) in hi_keys.iter().enumerate() {
        idx.put(k.as_bytes(), format!("h{i}").as_bytes()).unwrap();
    }
    assert_eq!(idx.global_depth(), 1);
    assert_eq!(idx.stats().unwrap().live_buckets, 2);

    // 全删：空桶与等深伙伴合并，目录最终收缩回 gd=0。
    for k in lo_keys.iter().chain(hi_keys.iter()) {
        assert!(idx.delete(k.as_bytes()).unwrap());
    }
    assert_eq!(idx.global_depth(), 0, "目录应收缩回 0");
    let stats = idx.stats().unwrap();
    assert_eq!(stats.total_records, 0);
    assert_eq!(stats.live_buckets, 1, "合并后只剩一个根桶");
    assert_eq!(stats.free_pages, 1, "另一个桶页应在空闲链表");

    // 回收桶页必须可被再利用：再插入同样 4 键，高水位不增长，记录数恢复。
    let hw_before = stats.next_bucket_id;
    for (i, k) in lo_keys.iter().enumerate() {
        idx.put(k.as_bytes(), format!("l{i}").as_bytes()).unwrap();
    }
    for (i, k) in hi_keys.iter().enumerate() {
        idx.put(k.as_bytes(), format!("h{i}").as_bytes()).unwrap();
    }
    let stats2 = idx.stats().unwrap();
    assert_eq!(stats2.next_bucket_id, hw_before, "应复用回收桶页");
    assert_eq!(stats2.total_records, 4);
}

#[test]
fn merge_recovery_during_redirect() {
    let path = temp_path("merge-crash");
    let mut idx = Index::create(&path, 2, HashKind::Fx).unwrap();
    let h = ehindex::hash::hasher_for(HashKind::Fx);
    // 构造 gd=1、两个叶子桶各 2 条（散列互异，避免空转分裂）。
    let mut lo = Vec::new();
    let mut hi = Vec::new();
    for k in 0..500000u32 {
        let key = format!("mc-{k}");
        let v = h.hash(key.as_bytes());
        let bucket = if v & 0x8000_0000 == 0 {
            &mut lo
        } else {
            &mut hi
        };
        if bucket.len() < 2 && !bucket.iter().any(|(vv, _)| *vv == v) {
            bucket.push((v, key));
        }
        if lo.len() == 2 && hi.len() == 2 {
            break;
        }
    }
    let zeros: Vec<String> = lo.iter().map(|(_, k)| k.clone()).collect();
    let ones: Vec<String> = hi.iter().map(|(_, k)| k.clone()).collect();
    for (i, k) in zeros.iter().enumerate() {
        idx.put(k.as_bytes(), format!("z{i}").as_bytes()).unwrap();
    }
    for (i, k) in ones.iter().enumerate() {
        idx.put(k.as_bytes(), format!("o{i}").as_bytes()).unwrap();
    }
    assert_eq!(idx.global_depth(), 1);

    // 低位桶删空：第一个不触发合并，第二个触发等深合并并在重定向一半时崩溃。
    idx.delete(zeros[0].as_bytes()).unwrap();
    idx.inject_fault(Fault::MergeDuringDirRedirect);
    assert_crashed(&idx.delete(zeros[1].as_bytes()));
    drop(idx);

    // 重放必然补完合并：gd 收缩回 0。
    let mut idx = Index::open(&path).unwrap();
    assert_eq!(idx.global_depth(), 0);
    assert_eq!(idx.get(zeros[0].as_bytes()).unwrap(), None);
    assert_eq!(idx.get(zeros[1].as_bytes()).unwrap(), None);
    assert_eq!(
        idx.get(ones[0].as_bytes()).unwrap().as_deref(),
        Some(b"o0".as_slice())
    );
    assert_eq!(
        idx.get(ones[1].as_bytes()).unwrap().as_deref(),
        Some(b"o1".as_slice())
    );
    // 之后可正常再插入。
    idx.put(zeros[0].as_bytes(), b"z0-again").unwrap();
    idx.put(zeros[1].as_bytes(), b"z1-again").unwrap();
    assert_eq!(
        idx.get(zeros[1].as_bytes()).unwrap().as_deref(),
        Some(b"z1-again".as_slice())
    );
}

#[test]
fn delete_nonexistent_is_idempotent() {
    let path = temp_path("del-missing");
    let mut idx = Index::create(&path, 2, HashKind::Fx).unwrap();
    assert!(!idx.delete(b"ghost").unwrap());
    idx.put(b"a", b"1").unwrap();
    assert!(idx.delete(b"a").unwrap());
    assert!(!idx.delete(b"a").unwrap());
}

// -------------------------------------------------------- 6. 与内存映射比对

#[test]
fn differential_against_hashmap_mixed_workload() {
    let path = temp_path("diff");
    let mut idx = Index::create(&path, 4, HashKind::Fx).unwrap();
    let mut map = HashMap::new();

    // 确定性伪随机 LCG 工作负载。
    let mut state: u64 = 0x1234_5678;
    let mut rnd = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (state >> 33) as u32
    };
    let keyspace = 400u32; // 键空间小于操作数 -> 大量更新/命中/分裂/合并
    for round in 0..4000u32 {
        let k = rnd() % keyspace;
        let op = rnd() % 100;
        let key = format!("K{k}");
        if op < 55 {
            // put
            let val = format!("round-{round}");
            idx.put(key.as_bytes(), val.as_bytes()).unwrap();
            map.insert(key.into_bytes(), val.into_bytes());
        } else if op < 80 {
            // get
            let got = idx.get(key.as_bytes()).unwrap();
            assert_eq!(
                got.as_deref(),
                map.get(key.as_bytes()).map(|v| v.as_slice())
            );
        } else {
            // delete
            let existed = idx.delete(key.as_bytes()).unwrap();
            assert_eq!(existed, map.remove(key.as_bytes()).is_some());
        }
    }
    assert_matches_map(&mut idx, &map);
    eprintln!(
        "差异测试结束：gd={} 存活桶={} 记录={}",
        idx.global_depth(),
        idx.stats().unwrap().live_buckets,
        map.len()
    );

    // 重开再比对一次。
    drop(idx);
    let mut reopened = Index::open(&path).unwrap();
    assert_matches_map(&mut reopened, &map);
}

// ---------------------------------------------------------- 7. CRC 损坏检测

#[test]
fn corrupt_page_is_rejected_with_clear_error() {
    let path = temp_path("corrupt");
    let mut idx = Index::create(&path, 2, HashKind::Fx).unwrap();
    idx.put(b"hello", b"world").unwrap();
    drop(idx);

    // 直接篡改 0 号桶页的中间字节。
    use std::io::{Seek, SeekFrom, Write};
    let mut f = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
    let off = ehindex::pager::FIRST_BUCKET_PAGE * 4096 + 100;
    f.seek(SeekFrom::Start(off)).unwrap();
    f.write_all(&[0xFF]).unwrap();
    drop(f);

    let err = match Index::open(&path) {
        Err(e) => e,
        Ok(_) => panic!("期望打开失败"),
    };
    match err {
        IndexError::Corrupt(msg) => assert!(msg.contains("CRC"), "应提示 CRC 失败：{msg}"),
        other => panic!("期望 Corrupt，得到 {other:?}"),
    }
}

#[test]
fn hash_mode_mismatch_is_rejected() {
    let path = temp_path("hash-mismatch");
    {
        let _i = Index::create(&path, 2, HashKind::LowMod(2)).unwrap();
    }
    let err = match Index::open_with(&path, Some(HashKind::Fx)) {
        Err(e) => e,
        Ok(_) => panic!("期望哈希模式不匹配错误"),
    };
    assert!(matches!(err, IndexError::HashModeMismatch { .. }));
    // 不指定模式（None）允许打开。
    let _i = Index::open(&path).unwrap();
}

#[test]
fn oversized_keys_and_values_are_rejected() {
    let path = temp_path("oversize");
    let mut idx = Index::create(&path, 2, HashKind::Fx).unwrap();
    assert!(matches!(
        idx.put(b"", b"v").unwrap_err(),
        IndexError::EmptyKey
    ));
    let big_key = vec![0u8; 129];
    assert!(matches!(
        idx.put(&big_key, b"v").unwrap_err(),
        IndexError::KeyTooLong { .. }
    ));
    let big_val = vec![0u8; 513];
    assert!(matches!(
        idx.put(b"k", &big_val).unwrap_err(),
        IndexError::ValueTooLong { .. }
    ));
}
