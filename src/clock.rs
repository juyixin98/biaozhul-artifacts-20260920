//! 可注入时钟：生产用系统时钟，测试用可控时钟，保证超时判断可确定性测试。

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

/// 毫秒级时钟。
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

/// 生产环境时钟：Unix 纪元毫秒。
#[derive(Default)]
pub struct SystemClock;

impl SystemClock {
    pub fn new() -> Self {
        Self
    }
}

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// 测试时钟：可任意拨动时间，用于确定性地制造预留超时。
pub struct FakeClock(AtomicI64);

impl FakeClock {
    pub fn new(initial_ms: i64) -> Self {
        Self(AtomicI64::new(initial_ms))
    }

    pub fn set(&self, ms: i64) {
        self.0.store(ms, Ordering::SeqCst);
    }

    /// 向前推进 `delta_ms` 毫秒。
    pub fn advance(&self, delta_ms: i64) {
        self.0.fetch_add(delta_ms, Ordering::SeqCst);
    }

    pub fn arc(initial_ms: i64) -> Arc<dyn Clock> {
        Arc::new(FakeClock::new(initial_ms))
    }
}

impl Clock for FakeClock {
    fn now_ms(&self) -> i64 {
        self.0.load(Ordering::SeqCst)
    }
}
