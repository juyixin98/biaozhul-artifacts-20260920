//! Crash-injection failpoints, enabled only via environment variables.
//!
//!   COW_CRASH_AT=<point>    crash (SIGABRT) when <point> is reached
//!   COW_CRASH_AFTER=<n>     crash on the n-th hit of that point (default 1)
//!
//! Points: page_flushed, page_committed, commit_tmp_written,
//!         manifest_committed, branch_committed, delete_committed.
//!
//! This simulates a power loss / kill -9 at an exact instruction boundary:
//! no destructors run, no cleanup happens.

use std::sync::atomic::{AtomicU64, Ordering};

static HITS: AtomicU64 = AtomicU64::new(0);

pub fn point(name: &str) {
    let Ok(target) = std::env::var("COW_CRASH_AT") else {
        return;
    };
    if target != name {
        return;
    }
    let after: u64 = std::env::var("COW_CRASH_AFTER")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(1);
    let n = HITS.fetch_add(1, Ordering::SeqCst) + 1;
    if n >= after {
        eprintln!("[failpoint] aborting at {name} (hit #{n})");
        std::process::abort();
    }
}
