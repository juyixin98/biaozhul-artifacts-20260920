//! 崩溃注入支持。
//!
//! 验收要求"在每个写入及同步边界注入崩溃"。实现方式:
//!
//! * [`SegmentLog`] 携带一个**默认空转**的崩溃钩子 [`CrashHook`], 生产 HTTP
//!   路径从不设置它;
//! * 集成测试以 `crash-worker` 子命令真正 **fork 一个子进程** 跑同一个二进制,
//!   子进程在指定边界调用 [`abrupt_exit`] —— `exit(9)` (或 `SIGKILL`),
//!   不运行任何析构函数、不刷新用户态缓冲, 等价于进程崩溃;
//! * 父进程等待子进程死亡后, 在同一目录上重新打开日志做恢复断言。
//!
//! 注入边界覆盖一次 append 的全部写/同步点:
//!
//! ```text
//! BeforeMarker ─ fsync(next.idx) ─ AfterMarker
//!                                      │  (必要时)
//!                          BeforeRoll ─ fsync(旧段)+建段+fsync(目录) ─ AfterRoll
//!                                      │
//! BeforeData ─ write_all(帧)+fsync(段) ─ AfterData
//! ```

use std::sync::Arc;

use crate::log::SegmentLog;

/// 一次 append 各边界的注入点。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    /// 序号 marker 写入/fsync 之前。
    BeforeMarker,
    /// 序号 marker fsync 成功之后、数据写入之前。
    AfterMarker,
    /// 段滚动之前。
    BeforeRoll,
    /// 段滚动(含目录 fsync)之后。
    AfterRoll,
    /// 数据帧 write_all + fsync 之前。
    BeforeData,
    /// 数据帧 fsync 成功之后、调用方拿到返回值之前。
    AfterData,
}

impl CrashPoint {
    pub fn as_str(self) -> &'static str {
        match self {
            CrashPoint::BeforeMarker => "before-marker",
            CrashPoint::AfterMarker => "after-marker",
            CrashPoint::BeforeRoll => "before-roll",
            CrashPoint::AfterRoll => "after-roll",
            CrashPoint::BeforeData => "before-data",
            CrashPoint::AfterData => "after-data",
        }
    }

    pub fn parse(s: &str) -> CrashPoint {
        match s {
            "before-marker" => CrashPoint::BeforeMarker,
            "after-marker" => CrashPoint::AfterMarker,
            "before-roll" => CrashPoint::BeforeRoll,
            "after-roll" => CrashPoint::AfterRoll,
            "before-data" => CrashPoint::BeforeData,
            "after-data" => CrashPoint::AfterData,
            other => panic!("unknown crash point: {other}"),
        }
    }
}

/// 钩子收到 (注入点, 第几次 append, 该次分配的序号)。
/// 命中规则时它通过 [`abrupt_exit`] 杀死本进程; 未命中则正常返回。
pub type CrashHook = Arc<dyn Fn(CrashPoint, u64, u64) + Send + Sync>;

/// 立即"崩溃": 不运行析构、不刷新缓冲。
///
/// 默认 `_exit(9)` 语义 (`std::process::exit` 不走 Drop)。设置
/// `SEGLOG_SIGKILL=1` 时改对自己发 `SIGKILL`, 更接近掉电/被 OOM 杀死。
pub fn abrupt_exit() -> ! {
    if std::env::var_os("SEGLOG_SIGKILL").is_some() {
        let _ = std::process::Command::new("kill")
            .arg("-9")
            .arg(std::process::id().to_string())
            .status();
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    std::process::exit(9)
}

/// 一个注入规则: 第 `ordinal` 次 append (从 1 开始) 到达 `point` 时崩溃。
#[derive(Debug, Clone, Copy)]
pub struct Rule {
    pub point: CrashPoint,
    pub ordinal: u64,
}

/// 解析 `before-data:3,after-marker:1` 形式的规则列表。
pub fn parse_rules(spec: &str) -> Vec<Rule> {
    spec.split(',')
        .filter(|s| !s.is_empty())
        .map(|s| {
            let (point, ordinal) = s
                .trim()
                .split_once(':')
                .unwrap_or_else(|| panic!("bad crash spec item: {s}"));
            Rule {
                point: CrashPoint::parse(point.trim()),
                ordinal: ordinal.trim().parse().expect("bad ordinal"),
            }
        })
        .collect()
}

/// crash-worker 子进程参数。
pub struct WorkerArgs {
    pub dir: String,
    pub seg_bytes: u64,
    pub appends: u64,
    pub rules: Vec<Rule>,
    pub dump: bool,
}

/// 解析 `--dir DIR --seg-bytes N --appends K --crash SPEC`。
pub fn parse_worker_args(mut args: impl Iterator<Item = String>) -> WorkerArgs {
    let mut dir = None;
    let mut seg_bytes = 4 * 1024u64;
    let mut appends = 0u64;
    let mut rules = Vec::new();
    let mut dump = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--dir" => dir = Some(args.next().expect("--dir needs value")),
            "--seg-bytes" => {
                seg_bytes = args
                    .next()
                    .expect("--seg-bytes needs value")
                    .parse()
                    .expect("bad seg-bytes")
            }
            "--appends" => {
                appends = args
                    .next()
                    .expect("--appends needs value")
                    .parse()
                    .expect("bad appends")
            }
            "--crash" => rules = parse_rules(&args.next().expect("--crash needs value")),
            "--dump" => dump = true,
            other => panic!("unknown crash-worker arg: {other}"),
        }
    }
    WorkerArgs {
        dir: dir.expect("missing --dir"),
        seg_bytes,
        appends,
        rules,
        dump,
    }
}

/// crash-worker 子进程主体。
///
/// * 默认(写模式): 打开日志, 装钩子, 顺序 append `appends` 条。每条确认的记录
///   打印 `COMMITTED ord=<i> seq=<i>`, 钩子命中则本进程立即消失。
/// * `--dump`(恢复核对模式): 打开日志并打印
///   `RECOVERED next=<next_seq> count=<n> seqs=1,2,5`,
///   打开失败则打印 `OPEN_FAILED <错误摘要>` 并以非 0 退出。
pub fn run_worker(args: WorkerArgs) -> ! {
    use std::io::Write;

    if args.dump {
        match SegmentLog::open(&args.dir, args.seg_bytes) {
            Ok(log) => {
                let next = log.next_seq();
                let records = log.read_all().expect("dump: read failed");
                let seqs: Vec<String> = records.iter().map(|r| r.seq.to_string()).collect();
                println!("RECOVERED next={next} count={} seqs={}", records.len(), seqs.join(","));
                std::io::stdout().flush().ok();
                std::process::exit(0);
            }
            Err(e) => {
                println!("OPEN_FAILED {e}");
                std::io::stdout().flush().ok();
                std::process::exit(3);
            }
        }
    }

    let rules = args.rules;
    let log = SegmentLog::open(&args.dir, args.seg_bytes).expect("worker: open failed");

    let hook: CrashHook = Arc::new(move |point, ordinal, _seq| {
        if rules.iter().any(|r| r.point == point && r.ordinal == ordinal) {
            eprintln!("[crash-worker] injected crash at {} (append #{ordinal})", point.as_str());
            abrupt_exit();
        }
    });
    log.set_crash_hook(Some(hook));

    println!("READY");
    std::io::stdout().flush().ok();

    for i in 1..=args.appends {
        let payload = format!("ord-{i:03}");
        let seq = log.append(payload.as_bytes()).expect("worker: append failed");
        // 本实现中 seq 从 1 连续分配 (无空洞除非发生认领后丢失), 故 seq==i。
        println!("COMMITTED ord={i} seq={seq}");
        std::io::stdout().flush().ok();
    }
    println!("DONE");
    std::process::exit(0)
}
