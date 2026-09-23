//! 崩溃恢复自检：在模拟磁盘上穷举一次提交的全部故障点，并构造
//! 半页写、双根损坏、旧根回退、越界指针等验收场景。
//!
//! `cargo test` 会逐条断言这些场景；HTTP `/admin/selftest` 与
//! `dual-superblock selftest` 复用 [`run`] 返回人类可读报告。

use crate::crc::Crc32;
use crate::format::{
    decode_super_page, encode_super_page, put_u32, put_u64, slot_for_generation, SuperBlock,
    PAGE_SIZE,
};
use crate::io_layer::{FaultKind, FaultRule, Op, SimDisk, Target};
use crate::store::{Repository, StoreError};

#[derive(Debug, Clone)]
pub struct CaseResult {
    pub name: String,
    pub passed: bool,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct Report {
    pub cases: Vec<CaseResult>,
}

impl Report {
    pub fn all_passed(&self) -> bool {
        self.cases.iter().all(|c| c.passed)
    }
    pub fn render_text(&self) -> String {
        let mut out = String::new();
        for c in &self.cases {
            let mark = if c.passed { "PASS" } else { "FAIL" };
            out.push_str(&format!("[{mark}] {}\n        {}\n", c.name, c.detail));
        }
        out.push_str(&format!(
            "\n共 {} 项，通过 {}，失败 {}\n",
            self.cases.len(),
            self.cases.iter().filter(|c| c.passed).count(),
            self.cases.iter().filter(|c| !c.passed).count()
        ));
        out
    }
}

fn case(name: &str, f: impl FnOnce() -> Result<String, String>) -> CaseResult {
    match f() {
        Ok(detail) => CaseResult {
            name: name.into(),
            passed: true,
            detail,
        },
        Err(detail) => CaseResult {
            name: name.into(),
            passed: false,
            detail,
        },
    }
}

fn expect_injected(e: &StoreError) -> Result<(), String> {
    if e.is_injected_fault() {
        Ok(())
    } else {
        Err(format!("应返回注入故障，实际为: {e}"))
    }
}

/// 格式化并做 `n` 次干净提交（每次写一个键 g1..gn）。
fn build(disk: &SimDisk, n: u64) -> Result<(), String> {
    let mut repo =
        Repository::format(disk.storage(vec![])).map_err(|e| format!("format 失败: {e}"))?;
    for g in 1..=n {
        let key = format!("g{g}");
        repo.put_batch([(key.as_bytes(), vec![g as u8; 8].as_slice())])
            .map_err(|e| format!("提交代次 {g} 失败: {e}"))?;
    }
    Ok(())
}

/// 用给定故障规则重新挂载并尝试一次单键提交；用完即“断电丢弃”该实例。
fn attempt_commit(
    disk: &SimDisk,
    rules: Vec<FaultRule>,
    key: &[u8],
    val: &[u8],
) -> Result<u64, StoreError> {
    let mut repo = Repository::open(disk.storage(rules))?;
    let snap = repo.put_batch([(key, val)])?;
    Ok(snap.generation)
}

fn reopened_generation(disk: &SimDisk) -> Result<u64, String> {
    let repo = Repository::open(disk.storage(vec![])).map_err(|e| format!("重新挂载失败: {e}"))?;
    Ok(repo.generation())
}

/// 运行全部自检用例。
pub fn run() -> Report {
    let mut cases = Vec::new();

    // ---- 1. 干净路径：5 代次，槽位严格交替，逐代可读 ----
    cases.push(case(
        "干净提交×5：双槽交替且全部键可读",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 5)?;
            let repo =
                Repository::open(disk.storage(vec![])).map_err(|e| format!("重挂载失败: {e}"))?;
            if repo.generation() != 5 {
                return Err(format!("期望代次 5，实际 {}", repo.generation()));
            }
            for g in 1..=5u64 {
                let key = format!("g{g}");
                match repo.get(key.as_bytes()) {
                    Some(v) if v == vec![g as u8; 8] => {}
                    other => return Err(format!("键 {key} 内容错误: {other:?}")),
                }
            }
            // 两个槽位应分别解码为代次 5（槽1）与 4（槽0）。
            let sf = disk.stable_super();
            let s0 = decode_super_page(&sf[0..PAGE_SIZE]).unwrap();
            let s1 = decode_super_page(&sf[PAGE_SIZE..2 * PAGE_SIZE]).unwrap();
            if s0.generation != 4 || s1.generation != 5 {
                return Err(format!(
                    "槽位代次错误: 槽0={} 槽1={}",
                    s0.generation, s1.generation
                ));
            }
            Ok("代次5已发布；槽0=代次4、槽1=代次5，符合交替规则".into())
        },
    ));

    // ---- 2. 崩溃点穷举：代次3→4 的一次提交，6 个注入点逐一崩溃 ----
    // 单键提交的操作序号：
    //   数据写: 1=LEAF, 2=ROOT；数据 sync: 1=叶子后, 2=根后；
    //   超级块写: 1；超级块 sync: 1。
    let crash_points: [(&str, Target, Op, u64); 6] = [
        ("写 LEAF 前", Target::Data, Op::Write, 1),
        ("LEAF 后 fsync 前", Target::Data, Op::Sync, 1),
        ("写 ROOT 前", Target::Data, Op::Write, 2),
        ("ROOT 后 fsync 前", Target::Data, Op::Sync, 2),
        ("写超级块页前", Target::Super, Op::Write, 1),
        ("超级块 fsync 前", Target::Super, Op::Sync, 1),
    ];
    for (label, target, op, nth) in crash_points {
        let label = label.to_string();
        cases.push(case(
            &format!("崩溃于「{label}」：恢复到上一完整代次3"),
            move || {
                let disk = SimDisk::fresh();
                build(&disk, 3)?;
                match attempt_commit(
                    &disk,
                    vec![FaultRule::crash(target, op, nth)],
                    b"g4",
                    &[4; 8],
                ) {
                    Ok(g) => Err(format!("注入崩溃后提交不应成功，却发布了代次 {g}")),
                    Err(e) => {
                        expect_injected(&e)?;
                        let g = reopened_generation(&disk)?;
                        if g != 3 {
                            return Err(format!("应回退到代次3，实际代次 {g}"));
                        }
                        let repo = Repository::open(disk.storage(vec![])).unwrap();
                        if repo.get(b"g4").is_some() {
                            return Err("未提交成功的键 g4 不应可见".into());
                        }
                        if repo.get(b"g3") != Some([3u8; 8].as_slice()) {
                            return Err("代次3 的数据应完好".into());
                        }
                        Ok("新代次未发布，代次3完整可读".into())
                    }
                }
            },
        ));
    }

    // ---- 3a. 半页写：超级块新页只落盘 8 字节（头部被截断）----
    cases.push(case(
        "半页写-超级块(保留8B)：新页头损坏，旧槽位接管",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 3)?;
            let err = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Super, 1, 8)],
                b"g4",
                &[4; 8],
            )
            .err()
            .ok_or_else(|| "应返回半页写故障".to_string())?;
            expect_injected(&err)?;
            let g = reopened_generation(&disk)?;
            if g != 3 {
                return Err(format!("应回退代次3，实际 {g}"));
            }
            Ok("交替槽位的旧超级块(代次3) CRC 完好，恢复成功".into())
        },
    ));

    // ---- 3b. 半页写：超级块新页落盘 2048 字节（头在但整页 CRC 不可能通过）----
    cases.push(case(
        "半页写-超级块(保留2048B)：页头CRC失败，拒绝半新页",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 3)?;
            let err = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Super, 1, 2048)],
                b"g4",
                &[4; 8],
            )
            .err()
            .ok_or_else(|| "应返回半页写故障".to_string())?;
            expect_injected(&err)?;
            let g = reopened_generation(&disk)?;
            if g != 3 {
                return Err(format!("应回退代次3，实际 {g}"));
            }
            Ok("半新页整页 CRC/尾戳校验失败，被明确拒绝".into())
        },
    ));

    // ---- 3c. 半页写恰好整页：写已完整落盘但 fsync 未回执即断电；
    //          整页 CRC 必然通过，重挂载应看到新代次 ----
    cases.push(case(
        "超级块写入保留4096B(整页)：报错但重挂载为代次4",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 3)?;
            let err = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Super, 1, PAGE_SIZE)],
                b"g4",
                &[4; 8],
            )
            .err()
            .ok_or_else(|| "TearWrite 即使整页落盘也应在 fsync 前报错".to_string())?;
            expect_injected(&err)?;
            let g = reopened_generation(&disk)?;
            if g != 4 {
                return Err(format!("整页已持久化，重挂载应到代次4，实际 {g}"));
            }
            let repo = Repository::open(disk.storage(vec![])).unwrap();
            if repo.get(b"g4") != Some([4u8; 8].as_slice()) {
                return Err("代次4 的数据应完好可读".into());
            }
            Ok("整页原子落盘；fsync 回执丢失不影响已发布内容".into())
        },
    ));

    // ---- 3d. 半页写：LEAF 记录只落盘一半（垃圾尾巴必须被容忍）----
    cases.push(case(
        "半页写-LEAF(保留3B)：残缺叶子成为垃圾尾巴，根未发布",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 2)?;
            let err = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Data, 1, 3)],
                b"g3",
                &[3; 8],
            )
            .err()
            .ok_or_else(|| "应返回半页写故障".to_string())?;
            expect_injected(&err)?;
            let g = reopened_generation(&disk)?;
            if g != 2 {
                return Err(format!("应回退代次2，实际 {g}"));
            }
            let data_len = disk.stable_data().len() as u64;
            // 残留的 3 字节垃圾不应影响打开。
            if data_len < 3 {
                return Err("半页前缀应已落盘".into());
            }
            Ok("数据文件含3字节残缺尾巴，扫描在损坏处停止，恢复代次2".into())
        },
    ));

    // ---- 3e. 半页写：ROOT 记录只落盘一半 ----
    cases.push(case(
        "半页写-ROOT(保留5B)：根半成品被忽略",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 2)?;
            let err = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Data, 2, 5)],
                b"g3",
                &[3; 8],
            )
            .err()
            .ok_or_else(|| "应返回半页写故障".to_string())?;
            expect_injected(&err)?;
            let g = reopened_generation(&disk)?;
            if g != 2 {
                return Err(format!("应回退代次2，实际 {g}"));
            }
            Ok("叶子已落盘但无超级块指向；ROOT 半成品 CRC 不符，恢复代次2".into())
        },
    ));

    // ---- 4. 两个超级块均损坏 ----
    cases.push(case(
        "两个根槽位同时损坏：明确报错而非静默",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 2)?;
            // 把两页头部魔数都打坏。
            disk.with_super(|sf| {
                sf[0] ^= 0xFF;
                sf[PAGE_SIZE] ^= 0xFF;
            });
            match Repository::open(disk.storage(vec![])) {
                Err(StoreError::CorruptStore(msg)) => {
                    if !msg.contains("两个超级块槽位均无效") {
                        return Err(format!("错误信息不符: {msg}"));
                    }
                    Ok(format!("打开被拒绝：{msg}"))
                }
                Ok(_) => Err("两个槽位均损坏时不应打开成功".to_string()),
                Err(e) => Err(format!("应报“两个超级块均无效”，实际错误类型不符: {e}")),
            }
        },
    ));

    // ---- 5. 新根引用损坏 → 旧根回退（旧根引用必须有效）----
    cases.push(case(
        "代次2的ROOT记录损坏：回退到引用有效的代次1",
        || {
            let disk = SimDisk::fresh();
            {
                let mut repo = Repository::format(disk.storage(vec![])).unwrap();
                repo.put_batch([(b"x".as_slice(), b"v1".as_slice())])
                    .unwrap(); // gen1 → 槽1
                repo.put_batch([(b"x".as_slice(), b"v2-longer-value".as_slice())])
                    .unwrap(); // gen2 → 槽0
            }
            // 代次2 是偶数代次，超级块在槽位0。找到它指向的 ROOT 记录，
            // 翻转 payload 内一个字节，使整条记录 CRC 不符。
            let sf = disk.stable_super();
            let sb2 = decode_super_page(&sf[0..PAGE_SIZE]).unwrap();
            if sb2.generation != 2 {
                return Err(format!(
                    "前置条件错误：槽0应为代次2，实际 {}",
                    sb2.generation
                ));
            }
            let off = sb2.root.offset as usize;
            disk.with_data(|d| d[off + 20] ^= 0xFF); // ROOT payload 内部翻转 → 整记录 CRC 不符
            let repo = Repository::open(disk.storage(vec![]))
                .map_err(|e| format!("应能用代次1恢复，却报错: {e}"))?;
            if repo.generation() != 1 {
                return Err(format!("应恢复到代次1，实际 {}", repo.generation()));
            }
            if repo.get(b"x") != Some(b"v1".as_slice()) {
                return Err(format!("代次1 的值应为 v1，实际 {:?}", repo.get(b"x")));
            }
            Ok("高代次候选引用校验失败，按序验证较旧代次并成功".into())
        },
    ));

    // ---- 6a. 越界根指针：最高代次越界 → 回退旧代次 ----
    cases.push(case(
        "越界根指针(远超EOF)：拒绝该候选并回退代次1",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 1)?; // gen1 在槽1
                              // 手工在槽0发布一个“代次6、根指针指向 9,000,000”的超级块。
            let bogus = SuperBlock {
                generation: 6,
                root: crate::format::Ptr {
                    offset: 9_000_000,
                    len: 64,
                    crc: 0x1234_5678,
                },
            };
            disk.with_super(|sf| sf[0..PAGE_SIZE].copy_from_slice(&encode_super_page(&bogus)));
            let repo = Repository::open(disk.storage(vec![]))
                .map_err(|e| format!("应回退到代次1，却报错: {e}"))?;
            if repo.generation() != 1 {
                return Err(format!("应恢复代次1，实际 {}", repo.generation()));
            }
            Ok("越界候选被拒，合法的代次1接管".into())
        },
    ));

    // ---- 6b. 越界指针 + 另一槽也损坏：必须报错，绝不解引用 ----
    cases.push(case(
        "唯一有效候选的根指针越界：报错且不解引用",
        || {
            let disk = SimDisk::fresh();
            {
                // 只 format（代次0在槽0），随后把槽0也改写成越界的高代次，
                // 槽1放另一个越界块——两个候选都必须被拒。
            }
            let _ = Repository::format(disk.storage(vec![])).unwrap();
            let bogus0 = SuperBlock {
                generation: 4,
                root: crate::format::Ptr {
                    offset: 1,
                    len: 16,
                    crc: 1,
                },
            };
            let bogus1 = SuperBlock {
                generation: 3,
                root: crate::format::Ptr {
                    offset: u64::MAX - 3,
                    len: 16,
                    crc: 2,
                },
            };
            disk.with_super(|sf| {
                sf[0..PAGE_SIZE].copy_from_slice(&encode_super_page(&bogus0));
                sf[PAGE_SIZE..2 * PAGE_SIZE].copy_from_slice(&encode_super_page(&bogus1));
            });
            match Repository::open(disk.storage(vec![])) {
                Err(StoreError::CorruptStore(msg)) if msg.contains("越界") => {
                    Ok(format!("越界指针被拒绝：{msg}"))
                }
                Ok(_) => Err("候选均越界时不应打开成功".to_string()),
                Err(e) => Err(format!("应报越界 CorruptStore，实际 {e}")),
            }
        },
    ));

    // ---- 7. 超级块代次与 ROOT 代次不一致（撕裂发布/手工伪造）----
    cases.push(case(
        "超级块代次与ROOT代次不符：拒绝并回退",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 2)?; // 代次2 在槽0，代次1 在槽1
                              // 把槽0页面中的代次改成 99，再按“CRC 字段清零”重算**整页** CRC，
                              // 使其通过页校验但与数据文件里 ROOT 记录的代次矛盾。
            disk.with_super(|sf| {
                put_u64(&mut sf[6..14], 99);
                let mut c = Crc32::new();
                c.update(&sf[..34]);
                c.update(&[0, 0, 0, 0]);
                c.update(&sf[38..]);
                put_u32(&mut sf[34..38], c.finalize());
            });
            let repo = Repository::open(disk.storage(vec![]))
                .map_err(|e| format!("应回退到代次1，却报错: {e}"))?;
            if repo.generation() != 1 {
                return Err(format!("应恢复代次1，实际 {}", repo.generation()));
            }
            Ok("代次矛盾候选被拒，槽1中的代次1仍然有效".into())
        },
    ));

    // ---- 8. 崩溃后重试可继续前进（liveness，不永久卡死）----
    cases.push(case(
        "崩溃后重新提交：代次继续推进且槽位内容正确",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 2)?;
            // 第一次提交在超级块 fsync 前崩。
            let e = attempt_commit(
                &disk,
                vec![FaultRule::crash(Target::Super, Op::Sync, 1)],
                b"g3",
                &[3; 8],
            )
            .err()
            .ok_or_else(|| "应崩溃".to_string())?;
            expect_injected(&e)?;
            // 不带故障重挂载，重做同一逻辑提交。
            let g = attempt_commit(&disk, vec![], b"g3", &[3; 8])
                .map_err(|e| format!("重试失败: {e}"))?;
            if g != 3 {
                return Err(format!("重试后应到代次3，实际 {g}"));
            }
            let repo = Repository::open(disk.storage(vec![])).unwrap();
            if repo.get(b"g3") != Some([3u8; 8].as_slice()) {
                return Err("代次3数据不可读".into());
            }
            // 槽位使用规则：代次3 必须在槽1。
            let sf = disk.stable_super();
            let s1 = decode_super_page(&sf[PAGE_SIZE..2 * PAGE_SIZE]).unwrap();
            if s1.generation != 3 || slot_for_generation(3) != 1 {
                return Err("代次3 槽位错误".into());
            }
            Ok("重试成功，代次推进到3".into())
        },
    ));

    // ---- 9. 故障类型本身可被区分（TearWrite 的 keep 语义）----
    cases.push(case(
        "故障可观测性：错误携带 target/op/nth/kind",
        || {
            let disk = SimDisk::fresh();
            build(&disk, 1)?;
            let e = attempt_commit(
                &disk,
                vec![FaultRule::tear(Target::Data, 2, 5)],
                b"g2",
                &[2; 8],
            )
            .err()
            .ok_or_else(|| "应故障".to_string())?;
            let f = e
                .injected_fault()
                .ok_or_else(|| "应识别为注入故障".to_string())?;
            if f.target != Target::Data || f.op != Op::Write || f.nth != 2 {
                return Err(format!("故障坐标错误: {f:?}"));
            }
            match f.kind {
                FaultKind::TearWrite(5) => Ok("TearWrite(5) 被准确定位在 数据文件第2次写".into()),
                k => Err(format!("故障类型错误: {k:?}")),
            }
        },
    ));

    Report { cases }
}
