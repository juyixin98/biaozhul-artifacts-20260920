use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

/// 可注入时钟：所有"预留是否超时"的判断只依赖该 trait，
/// 生产环境用系统时钟，测试 / 演示用固定可推进时钟。
pub trait Clock: Send + Sync {
    /// 当前时间（Unix 纪元起的毫秒数）。
    fn now_ms(&self) -> i64;
}

/// 系统墙钟（生产默认）。
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// 可被外部推进的固定时钟。内部保存一个基准毫秒值，
/// `advance_ms` 单调向前走（不允许回拨，避免超时状态出现歧义）。
pub struct InjectedClock {
    inner: Mutex<i64>,
}

impl InjectedClock {
    pub fn new(start_ms: i64) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(start_ms),
        })
    }

    pub fn advance(&self, delta_ms: i64) -> i64 {
        let mut t = self.inner.lock().unwrap();
        if delta_ms > 0 {
            *t += delta_ms;
        }
        *t
    }

    pub fn set(&self, ms: i64) -> i64 {
        let mut t = self.inner.lock().unwrap();
        if ms >= *t {
            *t = ms;
        }
        *t
    }
}

impl Clock for InjectedClock {
    fn now_ms(&self) -> i64 {
        *self.inner.lock().unwrap()
    }
}
