use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// A source of monotonic-ish time in milliseconds.
///
/// All elapsed-time math in the state machine goes through
/// [`elapsed_ms`], which is wrap- and regression-safe: a clock that moves
/// backwards (NTP step, u64 wrap in tests) yields an elapsed time of 0
/// rather than a huge bogus interval, so clock anomalies can never cause
/// a spurious watchdog reset.
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> u64;
}

/// Elapsed milliseconds from `since` to `now`, saturating to 0 when the
/// clock appears to have moved backwards or wrapped.
pub fn elapsed_ms(now: u64, since: u64) -> u64 {
    now.checked_sub(since).unwrap_or(0)
}

/// Wall-clock time (milliseconds since the Unix epoch).
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
}

/// A fully controllable clock for tests and demos. Time only changes when
/// explicitly advanced or set.
pub struct ManualClock {
    now: AtomicU64,
}

impl ManualClock {
    pub fn new(start_ms: u64) -> Self {
        ManualClock {
            now: AtomicU64::new(start_ms),
        }
    }

    pub fn advance_ms(&self, delta: u64) {
        // wrapping_add lets tests deliberately exercise u64 wraparound.
        self.now.fetch_add(delta, Ordering::SeqCst);
    }

    pub fn set_ms(&self, value: u64) {
        self.now.store(value, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> u64 {
        self.now.load(Ordering::SeqCst)
    }
}
