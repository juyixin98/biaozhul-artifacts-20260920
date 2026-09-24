use std::sync::Mutex;

/// 可控时钟：生产环境走系统时钟；验收/测试用手动时钟，
/// 且手动时钟的当前值持久化到 SQLite（meta 表），重启后可精确复现。
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
    fn mode(&self) -> &'static str;
    /// 仅手动时钟支持：把时间向前推进 delta_ms（不允许倒拨）。
    fn advance(&self, _delta_ms: i64) -> Result<i64, String> {
        Err("当前为 system 时钟模式，不支持推进时间（启动时使用 --clock manual）".into())
    }
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time before unix epoch")
            .as_millis() as i64
    }
    fn mode(&self) -> &'static str {
        "system"
    }
}

pub struct ManualClock {
    inner: Mutex<i64>,
}

impl ManualClock {
    pub fn new(seed_ms: i64) -> Self {
        Self {
            inner: Mutex::new(seed_ms),
        }
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> i64 {
        *self.inner.lock().unwrap()
    }
    fn mode(&self) -> &'static str {
        "manual"
    }
    fn advance(&self, delta_ms: i64) -> Result<i64, String> {
        if delta_ms < 0 {
            return Err("delta_ms 不能为负（时钟不允许倒拨）".into());
        }
        let mut g = self.inner.lock().unwrap();
        *g += delta_ms;
        Ok(*g)
    }
}
