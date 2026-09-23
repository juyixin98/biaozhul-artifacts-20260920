//! 崩溃恢复验收测试: 在一次 append 的每个写入/同步边界, 用真实子进程注入崩溃
//! (exit(9), 以及一组用 SIGKILL), 重启后核对:
//!
//! 1. 已确认 (打印过 COMMITTED) 的记录全部保留;
//! 2. 未确认记录允许存在 (AfterData) 或丢失;
//! 3. 已认领序号不复用 (RECOVERED 的 next 单调, 后续写入拿到更大序号);
//! 4. 损坏不会静默跳过 (中段损坏用例见 http_api.rs / 单元测试)。

use std::path::PathBuf;
use std::process::{Command, Stdio};

fn bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_seglog"))
}

fn fresh_dir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "seglog-it-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

struct Dump {
    next_seq: u64,
    count: usize,
    seqs: Vec<u64>,
}

fn run_worker(dir: &PathBuf, seg_bytes: u64, appends: u64, crash: Option<&str>, sigkill: bool) -> std::process::Output {
    let mut cmd = Command::new(bin());
    cmd.arg("crash-worker")
        .arg("--dir")
        .arg(dir)
        .arg("--seg-bytes")
        .arg(seg_bytes.to_string())
        .arg("--appends")
        .arg(appends.to_string());
    if let Some(spec) = crash {
        cmd.arg("--crash").arg(spec);
    }
    if sigkill {
        cmd.env("SEGLOG_SIGKILL", "1");
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    cmd.output().unwrap()
}

fn dump(dir: &PathBuf, seg_bytes: u64) -> Dump {
    let output = Command::new(bin())
        .args(["crash-worker", "--dump", "--dir"])
        .arg(dir)
        .arg("--seg-bytes")
        .arg(seg_bytes.to_string())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "dump (recovery open) failed: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    parse_dump(&String::from_utf8(output.stdout).unwrap())
}

fn expect_open_failure(dir: &PathBuf, seg_bytes: u64) -> String {
    let output = Command::new(bin())
        .args(["crash-worker", "--dump", "--dir"])
        .arg(dir)
        .arg("--seg-bytes")
        .arg(seg_bytes.to_string())
        .output()
        .unwrap();
    assert!(!output.status.success(), "open should have been rejected");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("OPEN_FAILED"), "expected OPEN_FAILED, got {stdout}");
    stdout.to_string()
}

fn parse_dump(s: &str) -> Dump {
    let line = s
        .lines()
        .find(|l| l.starts_with("RECOVERED"))
        .unwrap_or_else(|| panic!("no RECOVERED line in: {s}"));
    let mut next_seq = None;
    let mut count = None;
    let mut seqs = Vec::new();
    for tok in line.split_whitespace() {
        if let Some(v) = tok.strip_prefix("next=") {
            next_seq = Some(v.parse().unwrap());
        } else if let Some(v) = tok.strip_prefix("count=") {
            count = Some(v.parse().unwrap());
        } else if let Some(v) = tok.strip_prefix("seqs=") {
            seqs = v
                .split(',')
                .filter(|x| !x.is_empty())
                .map(|x| x.parse().unwrap())
                .collect();
        }
    }
    Dump {
        next_seq: next_seq.unwrap(),
        count: count.unwrap(),
        seqs,
    }
}

fn committed_seqs(out: &std::process::Output) -> Vec<u64> {
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| l.strip_prefix("COMMITTED "))
        .filter_map(|kv| {
            kv.split_whitespace()
                .find(|x| x.starts_with("seq="))
                .and_then(|x| x.strip_prefix("seq="))
                .map(|x| x.parse().unwrap())
        })
        .collect()
}

const ALL_POINTS: &[(&str, bool)] = &[
    // (注入点, 崩溃时该序号是否应已完成数据落盘)
    ("before-marker", false),
    ("after-marker", false),
    ("before-roll", false),
    ("after-roll", false),
    ("before-data", false),
    ("after-data", true),
];

/// 帧 = 16B 头 + 7B 载荷 = 23B; seg-bytes=40 时: 第 2 条写后达 46B,
/// 所以第 3 条 append 必然触发段滚动 -> BeforeRoll/AfterRoll 一定会命中。
const SEG: u64 = 40;

#[test]
fn crash_at_every_boundary_preserves_committed_and_never_reuses_seq() {
    for (point, data_durable_when_crash) in ALL_POINTS {
        let dir = fresh_dir(point);

        // 在第 3 条 append 的指定边界崩溃。
        let out = run_worker(&dir, SEG, 10, Some(&format!("{point}:3")), false);
        assert!(
            !out.status.success(),
            "[{point}] worker should have died"
        );
        let committed = committed_seqs(&out);
        assert_eq!(committed, vec![1, 2], "[{point}] unexpected committed set");

        // 重启恢复。
        let d = dump(&dir, SEG);

        // 已确认记录必须全部保留, 且无重复、保持升序。
        for s in &committed {
            assert!(d.seqs.contains(s), "[{point}] committed seq {s} lost after restart!");
        }
        let mut sorted = d.seqs.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted, d.seqs, "[{point}] recovered seqs not strictly sorted/unique");

        // 数据中的序号必须是 1..=k 的前缀 (序号按顺序认领, 不可能后面的在、前面的没认领)。
        let k = d.seqs.len() as u64;
        assert_eq!(d.seqs, (1..=k).collect::<Vec<_>>(), "[{point}] unexpected seq set");

        match *point {
            "before-marker" => {
                // 序号 3 尚未认领: next 仍是 3, 数据只有 1,2。
                assert_eq!(d.next_seq, 3, "[{point}]");
                assert_eq!(d.count, 2, "[{point}]");
            }
            "after-data" => {
                // fsync 已完成但客户端未确认: 记录允许存在。
                assert_eq!(d.next_seq, 4, "[{point}]");
                assert_eq!(d.count, 3, "[{point}] unconfirmed-but-durable record should be kept");
                assert!(*data_durable_when_crash);
            }
            _ => {
                // after-marker / before-roll / after-roll / before-data:
                // 序号 3 已认领但数据未确认 -> 数据只有 1,2, next=4, 序号 3 留空不复用。
                assert_eq!(d.next_seq, 4, "[{point}]");
                assert_eq!(d.count, 2, "[{point}] unconfirmed record may be lost");
            }
        }

        // 继续写入: 再跑一个不崩溃的 worker, append 5 条。
        let out2 = run_worker(&dir, SEG, 5, None, false);
        assert!(out2.status.success(), "[{point}] continuation worker failed: {}",
            String::from_utf8_lossy(&out2.stderr));
        let new_committed = committed_seqs(&out2);

        // 新序号必须全部 >= 崩溃时的 next (绝不复用旧序号)。
        for s in &new_committed {
            assert!(*s >= d.next_seq, "[{point}] reused sequence {s} (< {})!", d.next_seq);
        }

        let d2 = dump(&dir, SEG);
        // 旧数据一条都不能少。
        for s in &d.seqs {
            assert!(d2.seqs.contains(s), "[{point}] previously durable seq {s} vanished");
        }
        // 全部记录仍可读出, 序号严格递增, 且新老不相交。
        let old: std::collections::HashSet<u64> = d.seqs.iter().copied().collect();
        for s in &new_committed {
            assert!(!old.contains(s), "[{point}] reused seq {s}");
        }
        assert_eq!(d2.count, d2.seqs.len());
    }
}

#[test]
fn sigkill_variant_behaves_like_exit() {
    // 用 SIGKILL 在 after-marker 边界杀进程 (更接近掉电)。
    let dir = fresh_dir("sigkill");
    let out = run_worker(&dir, SEG, 10, Some("after-marker:4"), true);
    assert!(!out.status.success());
    // 被信号杀死时 status.code() 为 None。
    assert!(out.status.code().is_none(), "expected signal death");
    let committed = committed_seqs(&out);
    assert_eq!(committed, vec![1, 2, 3]);

    let d = dump(&dir, SEG);
    assert_eq!(d.seqs, vec![1, 2, 3]); // 已确认保留
    assert_eq!(d.next_seq, 5); // seq4 已认领(marker fsync 过)但数据丢失 -> 留空
}

#[test]
fn no_crash_baseline() {
    let dir = fresh_dir("baseline");
    let out = run_worker(&dir, SEG, 8, None, false);
    assert!(out.status.success());
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("DONE"));
    let d = dump(&dir, SEG);
    assert_eq!(d.seqs, (1..=8).collect::<Vec<_>>());
    assert_eq!(d.next_seq, 9);
    assert_eq!(d.count, 8);
}

#[test]
fn restart_is_idempotent_and_appends_continue_after_reopen() {
    // 多次无写重启, 状态不变。
    let dir = fresh_dir("reopen");
    run_worker(&dir, SEG, 3, None, false);
    let d1 = dump(&dir, SEG);
    let d2 = dump(&dir, SEG);
    assert_eq!(d1.next_seq, d2.next_seq);
    assert_eq!(d1.seqs, d2.seqs);

    // 中段损坏必须拒绝打开: 多段后翻转第一个封口段的一个字节。
    let dir2 = fresh_dir("corrupt-mid");
    run_worker(&dir2, SEG, 9, None, false);
    let segments = list_segment_files(&dir2);
    assert!(segments.len() >= 2);
    let first = &segments[0];
    let mut bytes = std::fs::read(first).unwrap();
    let idx = 16 + 1; // 第一条记录载荷区第 2 字节
    bytes[idx] ^= 0xFF;
    std::fs::write(first, bytes).unwrap();

    let msg = expect_open_failure(&dir2, SEG);
    assert!(msg.contains("crc mismatch"), "should name crc mismatch: {msg}");
}

fn list_segment_files(dir: &PathBuf) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| {
            let p = e.unwrap().path();
            (p.extension().is_some_and(|x| x == "seg")).then_some(p)
        })
        .collect();
    v.sort();
    v
}

#[test]
fn unconfirmed_tail_record_full_frame_can_survive_and_is_kept() {
    // after-data: 记录完整落盘但未确认 -> 重启必须保留 (允许存在),
    // 且随后的新写入拿到 seq 4 而非复用 3。
    let dir = fresh_dir("tail-survives");
    let _ = run_worker(&dir, SEG, 10, Some("after-data:3"), false);
    let d = dump(&dir, SEG);
    assert_eq!(d.seqs, vec![1, 2, 3]);
    assert_eq!(d.next_seq, 4);

    let out = run_worker(&dir, SEG, 2, None, false);
    let new_seqs = committed_seqs(&out);
    assert_eq!(new_seqs, vec![4, 5]);
}
