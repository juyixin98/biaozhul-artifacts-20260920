//! 崩溃注入:仅用于测试/演示,通过环境变量 `SEGLOG_CRASH` 启用。
//!
//! 取值:
//! - `after_write`  : 记录已 write(可能仅在页缓存),尚未 fsync 时崩溃
//! - `after_sync`   : 记录已 fsync 持久化,但尚未在内存中提交时崩溃
//! - `after_commit` : 记录已提交(序号已分配),HTTP 应答发出之前崩溃
//!
//! 触发时进程以退出码 137 直接退出(模拟 SIGKILL / 断电,不运行析构)。

use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    /// write 之后、fsync 之前。
    AfterWrite,
    /// fsync 之后、内存提交之前。
    AfterSync,
    /// 内存提交之后、HTTP 应答之前。
    AfterCommit,
}

static CRASH_POINT: OnceLock<Option<CrashPoint>> = OnceLock::new();

/// 从环境变量读取崩溃注入配置,进程启动时调用一次。
pub fn init_from_env() {
    let point = std::env::var("SEGLOG_CRASH").ok().and_then(|v| match v.as_str() {
        "after_write" => Some(CrashPoint::AfterWrite),
        "after_sync" => Some(CrashPoint::AfterSync),
        "after_commit" => Some(CrashPoint::AfterCommit),
        _ => {
            eprintln!("[seglog] 警告: 未知 SEGLOG_CRASH 取值 {v:?},忽略");
            None
        }
    });
    if let Some(p) = point {
        eprintln!("[seglog] 崩溃注入已启用: {p:?}");
    }
    let _ = CRASH_POINT.set(point);
}

/// 若当前点与注入点一致,立即退出进程(模拟崩溃)。
pub fn maybe_crash(point: CrashPoint) {
    if let Some(Some(p)) = CRASH_POINT.get() {
        if *p == point {
            eprintln!("[seglog] 崩溃注入触发: {point:?},进程退出(模拟掉电)");
            std::process::exit(137);
        }
    }
}
