//! Replay logging: every run leaves a structured, append-only decision log.
//!
//! Each [`ReplayEvent`] carries the run id, a monotonically increasing
//! sequence number, an event kind and a JSON detail object with the key
//! intermediate state and the *reason* for the decision (e.g. why a build
//! side was chosen, where truncation stopped, which budget fired). Events
//! are kept in memory on the job record and, when the service is configured
//! with a log directory, mirrored as JSON Lines to
//! `<log_dir>/<run_id>.jsonl` so a failing run can be replayed offline.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::error::{Error, Result};

/// One structured replay-log entry.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReplayEvent {
    /// Per-run monotonically increasing sequence number.
    pub seq: u64,
    /// Run identifier shared by every event of one job.
    pub run_id: String,
    /// Event kind: `plan`, `batch`, `truncated`, `complete`, `failed`, `resume`.
    pub event: String,
    /// Key intermediate state plus the rationale for the decision.
    pub detail: Value,
}

impl ReplayEvent {
    pub fn new(seq: u64, run_id: &str, event: &str, detail: Value) -> Self {
        Self {
            seq,
            run_id: run_id.to_string(),
            event: event.to_string(),
            detail,
        }
    }
}

/// Per-run event sink: memory copy plus optional JSONL file mirror.
pub struct ReplayLog {
    run_id: String,
    seq: AtomicU64,
    events: Vec<ReplayEvent>,
    file: Option<File>,
}

impl ReplayLog {
    /// Open a log for `run_id`; with `log_dir`, create `<dir>/<run_id>.jsonl`.
    pub fn open(run_id: &str, log_dir: Option<&Path>) -> Result<Self> {
        let file = match log_dir {
            Some(dir) => {
                std::fs::create_dir_all(dir).map_err(|e| {
                    Error::compute(format!("cannot create replay log dir {}: {e}", dir.display()))
                })?;
                let path = Self::path_for(dir, run_id);
                Some(
                    OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&path)
                        .map_err(|e| {
                            Error::compute(format!(
                                "cannot open replay log {}: {e}",
                                path.display()
                            ))
                        })?,
                )
            }
            None => None,
        };
        Ok(Self {
            run_id: run_id.to_string(),
            seq: AtomicU64::new(0),
            events: Vec::new(),
            file,
        })
    }

    pub fn path_for(dir: &Path, run_id: &str) -> PathBuf {
        dir.join(format!("{run_id}.jsonl"))
    }

    /// Append an event; failures to write the file mirror are surfaced as
    /// compute errors (the in-memory copy is always appended first).
    pub fn record(&mut self, event: &str, detail: Value) -> Result<()> {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let entry = ReplayEvent::new(seq, &self.run_id, event, detail);
        self.events.push(entry.clone());
        if let Some(file) = &mut self.file {
            let line = serde_json::to_string(&entry)
                .map_err(|e| Error::compute(format!("cannot serialise replay event: {e}")))?;
            writeln!(file, "{line}")
                .map_err(|e| Error::compute(format!("cannot write replay log: {e}")))?;
        }
        Ok(())
    }

    pub fn events(&self) -> &[ReplayEvent] {
        &self.events
    }

    pub fn into_events(self) -> Vec<ReplayEvent> {
        self.events
    }
}

/// Convenience: small JSON object from counter fields.
pub fn counters_json(c: &crate::operator::plan::JoinCounters) -> Value {
    json!({
        "pairs_considered": c.pairs_considered,
        "p1_comparisons": c.p1_comparisons,
        "p2_candidate_accesses": c.p2_candidate_accesses,
        "p2_word_probes": c.p2_word_probes,
        "output_rows": c.output_rows,
    })
}
