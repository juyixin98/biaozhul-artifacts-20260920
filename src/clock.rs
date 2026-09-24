use std::sync::atomic::{AtomicI64, Ordering};

/// Time source in milliseconds. All watchdog arithmetic goes through this
/// trait so that tests and acceptance runs can drive time deterministically.
///
/// Values are allowed to move backwards (clock rollback) and to wrap; the
/// state machine is written so that neither produces spurious resets.
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

/// Wall-clock time (production).
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or(0)
    }
}

/// Deterministic clock for tests and demos.
///
/// `advance` moves time forward (saturating); `set` may move it to any value,
/// including backwards (rollback) or across a wrap boundary.
#[derive(Debug)]
pub struct ManualClock {
    cur: AtomicI64,
}

impl ManualClock {
    pub fn new(start_ms: i64) -> Self {
        Self {
            cur: AtomicI64::new(start_ms),
        }
    }

    pub fn advance(&self, ms: i64) -> i64 {
        let mut prev = self.cur.load(Ordering::SeqCst);
        loop {
            let next = prev.saturating_add(ms);
            match self
                .cur
                .compare_exchange(prev, next, Ordering::SeqCst, Ordering::SeqCst)
            {
                Ok(_) => return next,
                Err(p) => prev = p,
            }
        }
    }

    pub fn set(&self, now_ms: i64) -> i64 {
        self.cur.store(now_ms, Ordering::SeqCst);
        now_ms
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> i64 {
        self.cur.load(Ordering::SeqCst)
    }
}
