use crate::clock::{elapsed_ms, Clock};
use crate::store::{ResetRecord, Store, TaskRow};
use serde::Serialize;
use std::sync::Arc;

/// Static configuration of the supervised device.
#[derive(Clone, Debug)]
pub struct Config {
    /// Names of the critical tasks that must all make progress.
    pub tasks: Vec<String>,
    /// Supervision window in milliseconds. Every task must advance its
    /// progress counter within each window for a feed to be accepted.
    pub window_ms: u64,
    /// Number of consecutive resets that triggers safe mode.
    pub reset_threshold: u32,
}

/// Live state of one supervised task.
#[derive(Clone, Debug, Serialize)]
pub struct TaskState {
    pub name: String,
    /// Last reported progress counter.
    pub counter: u64,
    /// Counter value latched at the start of the current window. Progress
    /// means `counter > baseline`; a heartbeat that repeats the same
    /// counter is not progress.
    pub baseline: u64,
    /// Clock time when the counter last increased.
    pub last_progress_ms: u64,
    /// Clock time of the most recent heartbeat (any counter value).
    pub last_heartbeat_ms: u64,
}

impl TaskState {
    /// Has this task made progress that is still inside the window?
    fn progressing(&self, now: u64, window_ms: u64) -> bool {
        self.counter > self.baseline && elapsed_ms(now, self.last_progress_ms) <= window_ms
    }
}

/// A persisted snapshot of the whole device, returned by `status`.
#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub safe_mode: bool,
    pub fault_generation: u64,
    pub consecutive_resets: u32,
    pub reset_threshold: u32,
    pub window_ms: u64,
    pub window_start_ms: u64,
    pub now_ms: u64,
    pub last_reset_reason: Option<String>,
    /// Number of times the clock was observed moving backwards.
    pub clock_anomalies: u64,
    pub task_progress_required: Vec<String>,
    pub tasks: Vec<TaskState>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FeedOk {
    pub fed: bool,
    pub window_start_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum FeedError {
    /// Device is in safe mode; feeding is impossible until a manual clear.
    SafeMode { fault_generation: u64 },
    /// One or more tasks did not advance within the window.
    Stalled { stalled: Vec<String> },
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum TickOutcome {
    /// Window still open, nothing to do.
    WithinWindow { remaining_ms: u64 },
    /// Window expired; a reset was recorded.
    Reset {
        reason: String,
        consecutive_resets: u32,
        safe_mode: bool,
        fault_generation: u64,
    },
    /// Device is in safe mode; supervision is suspended.
    SafeMode { fault_generation: u64 },
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum ClearError {
    NotInSafeMode,
    /// The clear request was issued against an older fault generation.
    StaleGeneration { current: u64 },
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum HeartbeatError {
    UnknownTask { task: String },
    /// A task's progress counter must never decrease.
    CounterRegression { task: String, last: u64, got: u64 },
}

#[derive(Debug, Clone, Serialize)]
pub struct HeartbeatOk {
    pub task: String,
    /// True when the counter increased (real progress), false when the
    /// heartbeat merely repeated the previous counter.
    pub progressed: bool,
    pub counter: u64,
}

/// The watchdog recovery state machine. Pure logic plus persistence through
/// [`Store`]; no hardware, no threads, all time comes from the injected
/// [`Clock`].
pub struct Watchdog {
    config: Config,
    store: Store,
    clock: Arc<dyn Clock>,
    tasks: Vec<TaskState>,
    safe_mode: bool,
    fault_generation: u64,
    consecutive_resets: u32,
    window_start_ms: u64,
    last_reset_reason: Option<String>,
    clock_anomalies: u64,
    last_now_ms: u64,
}

impl Watchdog {
    /// Load persisted state (if any) and reconcile it with the config.
    pub fn load(
        store: Store,
        config: Config,
        clock: Arc<dyn Clock>,
    ) -> crate::error::Result<Self> {
        let now = clock.now_ms();
        let device = store.load_device()?;
        let stored_tasks = store.load_tasks()?;

        let tasks = config
            .tasks
            .iter()
            .map(|name| {
                // Restore last known progress for known tasks; new tasks
                // start from zero. Baselines restart from the restored
                // counter so a host restart does not by itself look like
                // progress or like a stall.
                let restored = stored_tasks.iter().find(|t| &t.name == name);
                let counter = restored.map(|t| t.counter).unwrap_or(0);
                TaskState {
                    name: name.clone(),
                    counter,
                    baseline: counter,
                    last_progress_ms: restored.map(|t| t.last_progress_ms).unwrap_or(now),
                    last_heartbeat_ms: restored.map(|t| t.last_heartbeat_ms).unwrap_or(now),
                }
            })
            .collect();

        let (safe_mode, fault_generation, consecutive_resets, last_reset_reason) = match device {
            Some(d) => (
                d.safe_mode,
                d.fault_generation,
                d.consecutive_resets,
                d.last_reset_reason,
            ),
            None => (false, 0, 0, None),
        };

        let wd = Watchdog {
            config,
            store,
            clock,
            tasks,
            safe_mode,
            fault_generation,
            consecutive_resets,
            window_start_ms: now,
            last_reset_reason,
            clock_anomalies: 0,
            last_now_ms: now,
        };
        wd.persist()?;
        Ok(wd)
    }

    /// Current time with backwards-clock anomaly accounting.
    fn now(&mut self) -> u64 {
        let now = self.clock.now_ms();
        if now < self.last_now_ms {
            self.clock_anomalies += 1;
        } else {
            self.last_now_ms = now;
        }
        now
    }

    fn persist(&self) -> crate::error::Result<()> {
        self.store.save_device(
            self.safe_mode,
            self.fault_generation,
            self.consecutive_resets,
            self.last_reset_reason.as_deref(),
        )?;
        self.store.save_tasks(
            &self
                .tasks
                .iter()
                .map(|t| TaskRow {
                    name: t.name.clone(),
                    counter: t.counter,
                    last_progress_ms: t.last_progress_ms,
                    last_heartbeat_ms: t.last_heartbeat_ms,
                })
                .collect::<Vec<_>>(),
        )?;
        Ok(())
    }

    pub fn status(&mut self) -> Status {
        let now = self.now();
        Status {
            safe_mode: self.safe_mode,
            fault_generation: self.fault_generation,
            consecutive_resets: self.consecutive_resets,
            reset_threshold: self.config.reset_threshold,
            window_ms: self.config.window_ms,
            window_start_ms: self.window_start_ms,
            now_ms: now,
            last_reset_reason: self.last_reset_reason.clone(),
            clock_anomalies: self.clock_anomalies,
            task_progress_required: self.config.tasks.clone(),
            tasks: self.tasks.clone(),
        }
    }

    /// Record a heartbeat from a task. Accepted in every mode — including
    /// safe mode — because a heartbeat must never be able to change the
    /// recovery state by itself.
    pub fn heartbeat(
        &mut self,
        task: &str,
        counter: u64,
    ) -> Result<HeartbeatOk, HeartbeatError> {
        let now = self.now();
        let t = self
            .tasks
            .iter_mut()
            .find(|t| t.name == task)
            .ok_or_else(|| HeartbeatError::UnknownTask {
                task: task.to_string(),
            })?;
        if counter < t.counter {
            return Err(HeartbeatError::CounterRegression {
                task: task.to_string(),
                last: t.counter,
                got: counter,
            });
        }
        t.last_heartbeat_ms = now;
        let progressed = counter > t.counter;
        if progressed {
            t.counter = counter;
            t.last_progress_ms = now;
        }
        let _ = self.persist();
        Ok(HeartbeatOk {
            task: task.to_string(),
            progressed,
            counter,
        })
    }

    /// Attempt to feed (kick) the watchdog. Allowed only when every task
    /// advanced its counter within the current window.
    pub fn feed(&mut self) -> Result<FeedOk, FeedError> {
        let now = self.now();
        if self.safe_mode {
            return Err(FeedError::SafeMode {
                fault_generation: self.fault_generation,
            });
        }
        let stalled = self.stalled_tasks(now);
        if !stalled.is_empty() {
            return Err(FeedError::Stalled { stalled });
        }
        // Successful feed: latch new baselines, open a fresh window, and
        // prove liveness by clearing the consecutive-reset counter.
        for t in &mut self.tasks {
            t.baseline = t.counter;
        }
        self.window_start_ms = now;
        self.consecutive_resets = 0;
        let _ = self.persist();
        Ok(FeedOk {
            fed: true,
            window_start_ms: now,
        })
    }

    /// Supervision cycle. If the window expired without a successful feed,
    /// record a reset. Called periodically by the host (or explicitly via
    /// the API in manual-clock mode).
    pub fn tick(&mut self) -> TickOutcome {
        let now = self.now();
        if self.safe_mode {
            return TickOutcome::SafeMode {
                fault_generation: self.fault_generation,
            };
        }
        let elapsed = elapsed_ms(now, self.window_start_ms);
        if elapsed <= self.config.window_ms {
            return TickOutcome::WithinWindow {
                remaining_ms: self.config.window_ms - elapsed,
            };
        }
        let stalled = self.stalled_tasks(now);
        let reason = if stalled.is_empty() {
            "window expired without a feed attempt".to_string()
        } else {
            format!("tasks stalled: {}", stalled.join(", "))
        };
        self.record_reset(now, reason.clone());
        TickOutcome::Reset {
            reason,
            consecutive_resets: self.consecutive_resets,
            safe_mode: self.safe_mode,
            fault_generation: self.fault_generation,
        }
    }

    /// Manually clear safe mode. The request must be bound to the current
    /// fault generation; a request captured before a newer fault (or never
    /// issued at all) is rejected as stale.
    pub fn clear_safe_mode(&mut self, fault_generation: u64) -> Result<(), ClearError> {
        if !self.safe_mode {
            return Err(ClearError::NotInSafeMode);
        }
        if fault_generation != self.fault_generation {
            return Err(ClearError::StaleGeneration {
                current: self.fault_generation,
            });
        }
        let now = self.now();
        self.safe_mode = false;
        self.consecutive_resets = 0;
        self.window_start_ms = now;
        for t in &mut self.tasks {
            t.baseline = t.counter;
        }
        let _ = self.persist();
        Ok(())
    }

    pub fn reset_log(&self) -> crate::error::Result<Vec<ResetRecord>> {
        self.store.load_reset_log()
    }

    fn stalled_tasks(&self, now: u64) -> Vec<String> {
        self.tasks
            .iter()
            .filter(|t| !t.progressing(now, self.config.window_ms))
            .map(|t| t.name.clone())
            .collect()
    }

    fn record_reset(&mut self, now: u64, reason: String) {
        self.consecutive_resets += 1;
        if self.consecutive_resets >= self.config.reset_threshold && !self.safe_mode {
            // Entering safe mode starts a new fault generation. Any clear
            // request bound to an earlier generation is now invalid.
            self.safe_mode = true;
            self.fault_generation += 1;
        }
        self.last_reset_reason = Some(reason.clone());
        let snapshot = serde_json::to_string(&self.tasks).unwrap_or_default();
        let _ = self.store.append_reset_log(
            now,
            self.fault_generation,
            self.consecutive_resets,
            &reason,
            &snapshot,
        );
        // Device rebooted: tasks restart from their last known counters and
        // a fresh window opens.
        for t in &mut self.tasks {
            t.baseline = t.counter;
        }
        self.window_start_ms = now;
        let _ = self.persist();
    }
}
