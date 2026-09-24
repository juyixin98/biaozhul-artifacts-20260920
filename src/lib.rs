//! Host-side recovery state machine for a device firmware watchdog.
//!
//! The host supervises a set of critical firmware tasks. Each task reports a
//! monotonically increasing progress counter alongside its heartbeats. The
//! watchdog may only be kicked ("fed") when *every* task has advanced its
//! counter within the current window — a repeated heartbeat with an unchanged
//! counter is liveness of the transport, not progress of the task.
//!
//! If a supervision window expires without a successful feed, the device is
//! considered reset; the reset reason and the last known progress snapshot are
//! persisted. When consecutive resets reach a threshold the device enters
//! safe mode. Safe mode is never cleared by ordinary heartbeats or feeds —
//! only by a manual clear request bound to the current fault generation.
//!
//! No real hardware is touched: the "reset" and "feed" operations are state
//! transitions plus durable log records in SQLite.

pub mod api;
pub mod clock;
pub mod core;
pub mod store;

pub use clock::{Clock, ManualClock, SystemClock};
pub use core::{Config, Watchdog};
pub use store::Store;

use std::sync::{Arc, Mutex};

pub mod error {
    /// Boxed error used throughout the crate.
    pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
}

/// Selectable clock source.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClockMode {
    System,
    Manual,
}

/// Shared application state behind a mutex. All mutations are short and
/// synchronous (in-memory state machine + small SQLite writes), so a plain
/// `Mutex` is adequate and keeps the state machine deterministic.
pub struct App {
    pub watchdog: Mutex<Watchdog>,
    pub clock: Arc<dyn Clock>,
    /// Handle to the controllable clock when running in manual mode.
    pub manual_clock: Option<Arc<ManualClock>>,
    pub clock_mode: ClockMode,
}

impl App {
    /// Open (or create) the persistent store at `db_path` and rebuild the
    /// in-memory state machine from it. Restart counts, safe mode, fault
    /// generation and last progress all survive a process restart.
    pub fn open(
        db_path: &str,
        config: Config,
        clock: Arc<dyn Clock>,
        manual_clock: Option<Arc<ManualClock>>,
        clock_mode: ClockMode,
    ) -> error::Result<Arc<App>> {
        let store = Store::open(db_path)?;
        let watchdog = Watchdog::load(store, config, clock.clone())?;
        Ok(Arc::new(App {
            watchdog: Mutex::new(watchdog),
            clock,
            manual_clock,
            clock_mode,
        }))
    }
}
