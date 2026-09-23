//! 集成测试：在模拟磁盘上穷举“多次提交 × 每个故障点”，并覆盖
//! 多键批次、删除、代次单调性等额外不变量。

use dual_superblock::format::PAGE_SIZE;
use dual_superblock::io_layer::{FaultKind, FaultRule, Op, SimDisk, Target};
use dual_superblock::selftest;
use dual_superblock::store::Repository;

fn reopen(disk: &SimDisk) -> Repository<dual_superblock::io_layer::SimStorage> {
    Repository::open(disk.storage(vec![])).expect("重新挂载不应失败")
}

fn fresh_with_commits(n: u64) -> SimDisk {
    let disk = SimDisk::fresh();
    let mut repo = Repository::format(disk.storage(vec![])).unwrap();
    for g in 1..=n {
        let key = format!("key{g}");
        repo.put_batch([(key.as_bytes(), vec![g as u8; 4].as_slice())])
            .unwrap_or_else(|e| panic!("提交代次 {g} 失败: {e}"));
    }
    drop(repo);
    disk
}

/// 单键提交的 6 个故障点（详见 format 模块“同步边界”）。
fn all_crash_rules() -> [(Target, Op, u64); 6] {
    [
        (Target::Data, Op::Write, 1),
        (Target::Data, Op::Sync, 1),
        (Target::Data, Op::Write, 2),
        (Target::Data, Op::Sync, 2),
        (Target::Super, Op::Write, 1),
        (Target::Super, Op::Sync, 1),
    ]
}
/// 对“代次 base→base+1”的单键提交，在每个故障点注入断电：
/// 提交必须报注入故障；重挂载必须严格停留在 base。
#[test]
fn crash_matrix_single_key_commits() {
    for base in 0..=4u64 {
        for (target, op, nth) in all_crash_rules() {
            let key = format!("next{base}");

            // 带故障尝试提交 base→base+1。
            let disk = fresh_with_commits(base);
            let attempt = {
                let st = disk.storage(vec![FaultRule::crash(target, op, nth)]);
                let mut repo = Repository::open(st).unwrap();
                repo.put_batch([(key.as_bytes(), b"v".as_slice())])
            };
            let err = attempt.expect_err("注入断电后提交必须失败");
            assert!(err.is_injected_fault(), "应为注入故障，实际 {err}");

            let repo = reopen(&disk);
            assert_eq!(
                repo.generation(),
                base,
                "base={base} 故障点={target:?}/{op:?}#{nth}：恢复代次应为 {base}"
            );
            // 全部既有键完好。
            for g in 1..=base {
                let k = format!("key{g}");
                assert_eq!(
                    repo.get(k.as_bytes()),
                    Some(vec![g as u8; 4].as_slice()),
                    "base={base} 故障点={target:?}/{op:?}#{nth}：键 {k} 数据损坏"
                );
            }
            assert!(repo.get(key.as_bytes()).is_none(), "崩溃的新键不得可见");
        }
    }
}

/// 同样的 6 个故障点，对“3 键批次”提交（数据写次数增加为 4：3 LEAF + 1 ROOT）。
#[test]
fn crash_matrix_three_key_batch() {
    // 批次：leaf×3, sync1, root, sync2, super write, super sync
    let rules: [FaultRule; 8] = [
        FaultRule::crash(Target::Data, Op::Write, 1),
        FaultRule::crash(Target::Data, Op::Write, 3),
        FaultRule::crash(Target::Data, Op::Sync, 1),
        FaultRule::crash(Target::Data, Op::Write, 4),
        FaultRule::crash(Target::Data, Op::Sync, 2),
        FaultRule::crash(Target::Super, Op::Write, 1),
        FaultRule::crash(Target::Super, Op::Sync, 1),
        FaultRule::crash(Target::Data, Op::Write, 2),
    ];
    for rule in rules {
        let disk = fresh_with_commits(2);
        let items = [
            (b"a".as_slice(), b"1".as_slice()),
            (b"b".as_slice(), b"22".as_slice()),
            (b"c".as_slice(), b"333".as_slice()),
        ];
        let err = {
            let mut repo = Repository::open(disk.storage(vec![rule])).unwrap();
            repo.put_batch(items).unwrap_err()
        };
        assert!(err.is_injected_fault());
        let repo = reopen(&disk);
        assert_eq!(repo.generation(), 2);
        assert!(repo.get(b"a").is_none());
        assert!(repo.get(b"b").is_none());
        assert!(repo.get(b"c").is_none());
        assert_eq!(repo.get(b"key1"), Some([1u8; 4].as_slice()));
    }
}

/// 半页写 keep 扫描：超级块页只落盘前 keep 字节，其余区域是**另一代次旧页
/// 残留**（非零、内容不同）时，任何 keep < PAGE_SIZE 都必须被整页 CRC/尾戳
/// 拒绝，恢复停在旧代次。
#[test]
fn torn_super_page_sweep() {
    for keep in [0usize, 1, 8, 33, 37, 38, 100, 2048, 4077, 4078, 4090, 4095] {
        let disk = fresh_with_commits(3); // gen3 在槽1；gen4 将写槽0
        let err = {
            let st = disk.storage(vec![FaultRule::tear(Target::Super, 1, keep)]);
            let mut repo = Repository::open(st).unwrap();
            repo.put_batch([(b"k".as_slice(), b"v".as_slice())])
                .unwrap_err()
        };
        assert!(err.is_injected_fault(), "keep={keep}");

        // tear 后未覆盖的尾部仍是“新介质零页”。为模拟真实介质上“尾部是
        // 上一代次页残留”（掉电时未写扇区不确定），向 keep..4096 注入垃圾。
        if keep < PAGE_SIZE {
            disk.with_super(|sf| {
                for b in sf.iter_mut().take(PAGE_SIZE).skip(keep) {
                    *b = 0xA5;
                }
            });
        }
        let repo = reopen(&disk);
        assert_eq!(repo.generation(), 3, "keep={keep} 时不应接受半新页");
    }

    // 边界情形：缺失的恰好是末尾保留区且介质返回的仍是全零（与完整新页逐字节
    // 不可区分）。这在信息论上无法被任何校验和检出——记录该现象，并断言
    // 此时恢复到的 gen4 数据本身完好（根已先 fsync，接受它仍然正确）。
    let disk = fresh_with_commits(3);
    let _ = {
        let st = disk.storage(vec![FaultRule::tear(Target::Super, 1, 4095)]);
        let mut repo = Repository::open(st).unwrap();
        repo.put_batch([(b"k".as_slice(), b"v".as_slice())])
            .unwrap_err()
    };
    let repo = reopen(&disk);
    assert_eq!(repo.generation(), 4);
    assert_eq!(repo.get(b"k"), Some(b"v".as_slice()));

    // keep == 4096：整页持久化，重挂载看到 gen4（尽管 fsync 回执丢失）。
    let disk = fresh_with_commits(3);
    let _ = {
        let st = disk.storage(vec![FaultRule::tear(Target::Super, 1, 4096)]);
        let mut repo = Repository::open(st).unwrap();
        repo.put_batch([(b"k".as_slice(), b"v".as_slice())])
            .unwrap_err()
    };
    assert_eq!(reopen(&disk).generation(), 4);
}

/// 数据文件半页写扫描：LEAF/ROOT 任意位置撕裂都必须停留在旧代次，
/// 且数据文件中残留的垃圾前缀不得让挂载失败。
#[test]
fn torn_data_record_sweep() {
    // gen3→4 单键：写1=LEAF，写2=ROOT；keep 从 0 到记录全长-1。
    for write_nth in [1u64, 2] {
        for keep in [0usize, 1, 3, 5, 9, 15, 16, 20] {
            let disk = fresh_with_commits(3);
            let err = {
                let st = disk.storage(vec![FaultRule::tear(Target::Data, write_nth, keep)]);
                let mut repo = Repository::open(st).unwrap();
                repo.put_batch([(b"g4".as_slice(), &[4u8; 8])]).unwrap_err()
            };
            assert!(err.is_injected_fault(), "写#{write_nth} keep={keep}");
            let repo = reopen(&disk);
            assert_eq!(repo.generation(), 3, "写#{write_nth} keep={keep}");
        }
    }
}

/// 删除操作本身也是一次新代次提交，崩溃语义与写入一致。
#[test]
fn delete_is_a_commit_and_recovers() {
    let disk = fresh_with_commits(2);
    // 删除 key1 时在超级块 fsync 前崩溃 → key1 仍在。
    let err = {
        let st = disk.storage(vec![FaultRule::crash(Target::Super, Op::Sync, 1)]);
        let mut repo = Repository::open(st).unwrap();
        repo.delete_batch([b"key1"]).unwrap_err()
    };
    assert!(err.is_injected_fault());
    let repo = reopen(&disk);
    assert_eq!(repo.generation(), 2);
    assert!(repo.get(b"key1").is_some());

    // 无故障重试删除 → 新代次，key1 消失，key2 保留。
    {
        let mut repo = reopen(&disk);
        let snap = repo.delete_batch([b"key1"]).unwrap();
        assert_eq!(snap.generation, 3);
    }
    let repo = reopen(&disk);
    assert!(repo.get(b"key1").is_none());
    assert!(repo.get(b"key2").is_some());
}

/// 覆盖更新：旧 LEAF 成为垃圾但仍在文件中；ROOT 只引用最新 LEAF。
#[test]
fn overwrite_grows_data_but_only_latest_is_reachable() {
    let disk = SimDisk::fresh();
    let data_lens: Vec<usize>;
    {
        let mut repo = Repository::format(disk.storage(vec![])).unwrap();
        repo.put_batch([(b"k".as_slice(), b"short".as_slice())])
            .unwrap();
        let l1 = disk.stable_data().len();
        repo.put_batch([(b"k".as_slice(), b"a-much-longer-value".as_slice())])
            .unwrap();
        let l2 = disk.stable_data().len();
        repo.put_batch([(b"k".as_slice(), b"x".as_slice())])
            .unwrap();
        let l3 = disk.stable_data().len();
        data_lens = vec![l1, l2, l3];
    }
    assert!(
        data_lens[0] < data_lens[1] && data_lens[1] < data_lens[2],
        "只追加，文件单调增长"
    );
    let repo = reopen(&disk);
    assert_eq!(repo.generation(), 3);
    assert_eq!(repo.get(b"k"), Some(b"x".as_slice()));
}

/// selftest 报告必须全绿（防止用例之间状态串扰）。
#[test]
fn selftest_report_all_pass() {
    let report = selftest::run();
    assert!(
        report.all_passed(),
        "自检存在失败项:\n{}",
        report
            .cases
            .iter()
            .filter(|c| !c.passed)
            .map(|c| format!("  FAIL {} — {}", c.name, c.detail))
            .collect::<Vec<_>>()
            .join("\n")
    );
    assert_eq!(report.cases.len(), 19);
}

#[test]
fn injected_fault_kind_is_observable() {
    let disk = fresh_with_commits(1);
    let err = {
        let st = disk.storage(vec![FaultRule::tear(Target::Data, 1, 7)]);
        let mut repo = Repository::open(st).unwrap();
        repo.put_batch([(b"z".as_slice(), b"z".as_slice())])
            .unwrap_err()
    };
    let f = err.injected_fault().expect("应能取出注入故障信息");
    assert_eq!(f.target, Target::Data);
    assert_eq!(f.op, Op::Write);
    assert!(matches!(f.kind, FaultKind::TearWrite(7)));
}
