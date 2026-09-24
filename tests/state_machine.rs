//! State-machine tests with a fully controllable clock.
//!
//! These cover the requirements directly: a stuck task, a short jitter that
//! recovers inside the window, clock regression/wraparound, safe-mode entry,
//! proof that ordinary heartbeats cannot clear safe mode, generation-bound
//! manual clears (including stale ones), and persistence across a simulated
//! restart.

use std::sync::Arc;
use watchdog_host::clock::ManualClock;
use watchdog_host::core::{Config, Watchdog};
use watchdog_host::store::Store;

const TASKS: [&str; 3] = ["control-loop", "sensor-fusion", "logger"];

fn test_config() -> Config {
    Config {
        tasks: TASKS.iter().map(|s| s.to_string()).collect(),
        window_ms: 1000,
        reset_threshold: 3,
    }
}

/// Build a watchdog whose clock starts at 10_000 ms (well away from zero so
/// wraparound tests mean something).
fn make() -> (Watchdog, Arc<ManualClock>) {
    let clock = Arc::new(ManualClock::new(10_000));
    let store = Store::open_memory().unwrap();
    let wd = Watchdog::load(store, test_config(), clock.clone()).unwrap();
    (wd, clock)
}

fn hb(wd: &mut Watchdog, task: &str, counter: u64) {
    wd.heartbeat(task, counter).unwrap_or_else(|e| {
        panic!("heartbeat {task}={counter} failed: {e:?}")
    });
}

/// Every task reports counter=1 and the feed succeeds.
fn all_progress_and_feed(wd: &mut Watchdog) {
    for (i, t) in TASKS.iter().enumerate() {
        hb(wd, t, (i + 1) as u64);
    }
    wd.feed().expect("feed should succeed when all tasks progress");
}

#[test]
fn repeated_heartbeat_is_not_progress() {
    let (mut wd, clock) = make();
    // All tasks report progress once...
    for (i, t) in TASKS.iter().enumerate() {
        hb(&mut wd, t, (i + 1) as u64);
    }
    wd.feed().unwrap();
    clock.advance_ms(500);
    // ...then every task keeps heartbeating with the SAME counter.
    for (i, t) in TASKS.iter().enumerate() {
        let ok = wd.heartbeat(t, (i + 1) as u64).unwrap();
        assert!(!ok.progressed, "repeated counter must not be progress");
    }
    let err = wd.feed().expect_err("feed must be rejected without progress");
    match err {
        watchdog_host::core::FeedError::Stalled { stalled } => {
            assert_eq!(stalled.len(), 3, "all three tasks are stalled: {stalled:?}");
        }
        other => panic!("expected stalled, got {other:?}"),
    }
}

#[test]
fn one_task_stuck_triggers_resets_then_safe_mode() {
    let (mut wd, clock) = make();

    // Window 1: only two of three tasks advance. logger is stuck at 0.
    for t in ["control-loop", "sensor-fusion"] {
        hb(&mut wd, t, 1);
    }
    assert!(wd.feed().is_err(), "feed must fail while logger is stuck");

    clock.advance_ms(1001);
    let r1 = wd.tick();
    assert!(matches!(
        r1,
        watchdog_host::core::TickOutcome::Reset {
            consecutive_resets: 1,
            safe_mode: false,
            ..
        }
    ));

    // Windows 2 and 3: still stuck. Heartbeats from the stuck task with an
    // unchanged counter must not rescue anything.
    for expected in 2u32..=3 {
        hb(&mut wd, "logger", 0);
        hb(&mut wd, "control-loop", 10);
        clock.advance_ms(1001);
        let r = wd.tick();
        match r {
            watchdog_host::core::TickOutcome::Reset {
                consecutive_resets,
                safe_mode,
                fault_generation,
                ..
            } => {
                assert_eq!(consecutive_resets, expected);
                assert_eq!(safe_mode, expected == 3);
                assert_eq!(fault_generation, u64::from(expected == 3));
            }
            other => panic!("expected reset, got {other:?}"),
        }
    }

    let status = wd.status();
    assert!(status.safe_mode);
    assert_eq!(status.fault_generation, 1);
    assert!(status
        .last_reset_reason
        .as_deref()
        .unwrap()
        .contains("logger"));

    // The reset journal retains reasons and the last-progress snapshot.
    let log = wd.reset_log().unwrap();
    assert_eq!(log.len(), 3);
    let snapshot: serde_json::Value = serde_json::from_str(&log[2].task_snapshot).unwrap();
    let logger = snapshot
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "logger")
        .unwrap();
    assert_eq!(logger["counter"], 0, "last progress of stuck task retained");
    assert_eq!(
        snapshot.as_array().unwrap()[0]["counter"],
        if TASKS[0] == "control-loop" { 10 } else { 0 }
    );
}

#[test]
fn safe_mode_cannot_be_cleared_by_heartbeats_or_feeds() {
    let (mut wd, clock) = make();
    // Reach safe mode.
    for _ in 0..3 {
        clock.advance_ms(1001);
        match wd.tick() {
            watchdog_host::core::TickOutcome::Reset { .. } => {}
            other => panic!("expected reset: {other:?}"),
        }
    }
    assert!(wd.status().safe_mode);

    // Lots of time passes, every task makes genuine progress and keeps
    // heartbeating: nothing ordinary must clear safe mode.
    for round in 1u64..=5 {
        clock.advance_ms(2000);
        for (i, t) in TASKS.iter().enumerate() {
            hb(&mut wd, t, round * 10 + i as u64);
        }
        match wd.feed() {
            Err(watchdog_host::core::FeedError::SafeMode { .. }) => {}
            other => panic!("feed in safe mode must be rejected as SafeMode, got {other:?}"),
        }
        match wd.tick() {
            watchdog_host::core::TickOutcome::SafeMode { .. } => {}
            other => panic!("tick in safe mode must report SafeMode, got {other:?}"),
        }
    }
    assert!(wd.status().safe_mode, "safe mode persists despite progress");
}

#[test]
fn manual_clear_requires_current_generation() {
    let (mut wd, clock) = make();
    for _ in 0..3 {
        clock.advance_ms(1001);
        wd.tick();
    }
    assert_eq!(wd.status().fault_generation, 1);

    // A stale request (captured before the current fault) is invalid.
    match wd.clear_safe_mode(0) {
        Err(watchdog_host::core::ClearError::StaleGeneration { current }) => {
            assert_eq!(current, 1);
        }
        other => panic!("expected stale, got {other:?}"),
    }
    // A speculative future generation is invalid too.
    assert!(matches!(
        wd.clear_safe_mode(99),
        Err(watchdog_host::core::ClearError::StaleGeneration { .. })
    ));
    assert!(wd.status().safe_mode);

    // Correct generation clears it.
    wd.clear_safe_mode(1).unwrap();
    let s = wd.status();
    assert!(!s.safe_mode);
    assert_eq!(s.consecutive_resets, 0);

    // After clearing, normal operation resumes and a feed works.
    all_progress_and_feed(&mut wd);
}

#[test]
fn fault_generation_invalidates_old_clear_after_re_entry() {
    let (mut wd, clock) = make();

    // First fault episode -> generation 1, cleared correctly.
    for _ in 0..3 {
        clock.advance_ms(1001);
        wd.tick();
    }
    wd.clear_safe_mode(1).unwrap();

    // Second episode: three more consecutive resets -> generation 2.
    for _ in 0..3 {
        clock.advance_ms(1001);
        wd.tick();
    }
    assert_eq!(wd.status().fault_generation, 2);
    // A replay of the old, once-valid clear request is now rejected.
    assert!(matches!(
        wd.clear_safe_mode(1),
        Err(watchdog_host::core::ClearError::StaleGeneration { current: 2 })
    ));
    wd.clear_safe_mode(2).unwrap();
    assert!(!wd.status().safe_mode);
}

#[test]
fn successful_feed_resets_consecutive_count_and_tolerates_jitter() {
    let (mut wd, clock) = make();

    // Window 1 expires with no feed -> 1 reset.
    clock.advance_ms(1001);
    assert!(matches!(
        wd.tick(),
        watchdog_host::core::TickOutcome::Reset {
            consecutive_resets: 1,
            ..
        }
    ));

    // Brief jitter: tasks are late but progress and feed BEFORE the window
    // boundary (window is inclusive at exactly window_ms after reset).
    clock.advance_ms(900);
    for (i, t) in TASKS.iter().enumerate() {
        hb(&mut wd, t, (i + 1) as u64);
    }
    wd.feed().unwrap();
    assert_eq!(wd.status().consecutive_resets, 0, "a good window proves liveness");

    // Even progress at the exact boundary counts (<= window).
    clock.advance_ms(1000);
    for (i, t) in TASKS.iter().enumerate() {
        hb(&mut wd, t, 100 + i as u64);
    }
    wd.feed().expect("progress exactly at window edge must be accepted");
}

#[test]
fn clock_going_backwards_never_causes_spurious_reset() {
    let (mut wd, clock) = make();
    all_progress_and_feed(&mut wd);

    // Clock regresses sharply (NTP-style step).
    clock.set_ms(5_000);
    match wd.tick() {
        watchdog_host::core::TickOutcome::WithinWindow { remaining_ms } => {
            assert_eq!(remaining_ms, 1000, "backwards clock is treated as 0 elapsed");
        }
        other => panic!("backwards clock must not reset: {other:?}"),
    }
    assert!(
        wd.status().clock_anomalies >= 1,
        "backwards clock step must be counted"
    );

    // Time moves forward but remains below the previous high-water mark:
    // elapsed time stays 0 and the window simply continues.
    clock.advance_ms(500);
    assert!(matches!(
        wd.tick(),
        watchdog_host::core::TickOutcome::WithinWindow { .. }
    ));

    // u64 wraparound is handled the same way.
    clock.set_ms(u64::MAX);
    clock.advance_ms(10); // wraps to 9
    match wd.tick() {
        watchdog_host::core::TickOutcome::WithinWindow { .. } => {}
        other => panic!("wrapped clock must not reset: {other:?}"),
    }
}

#[test]
fn progress_counter_cannot_regress() {
    let (mut wd, _clock) = make();
    hb(&mut wd, "logger", 5);
    match wd.heartbeat("logger", 4) {
        Err(watchdog_host::core::HeartbeatError::CounterRegression { last, got, .. }) => {
            assert_eq!((last, got), (5, 4));
        }
        other => panic!("expected regression error, got {other:?}"),
    }
}

#[test]
fn unknown_task_is_rejected() {
    let (mut wd, _clock) = make();
    assert!(matches!(
        wd.heartbeat("nope", 1),
        Err(watchdog_host::core::HeartbeatError::UnknownTask { .. })
    ));
}

/// Full lifecycle persisted to a real file and recovered by a fresh
/// `Watchdog` instance (simulating a process restart).
#[test]
fn restart_persists_safe_mode_generation_counts_and_progress() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("wd.db");

    let clock = Arc::new(ManualClock::new(10_000));
    {
        let store = Store::open(&db).unwrap();
        let mut wd = Watchdog::load(store, test_config(), clock.clone()).unwrap();
        for (i, t) in TASKS.iter().enumerate() {
            hb(&mut wd, t, (i + 1) as u64);
        }
        for _ in 0..3 {
            clock.advance_ms(1001);
            wd.tick();
        }
        assert!(wd.status().safe_mode);
        assert_eq!(wd.status().fault_generation, 1);
    } // wd dropped: process "exits"

    // "Restart": a new instance over the same database at a much later time.
    clock.set_ms(99_000);
    let store = Store::open(&db).unwrap();
    let mut wd = Watchdog::load(store, test_config(), clock.clone()).unwrap();

    let s = wd.status();
    assert!(s.safe_mode, "safe mode survives restart");
    assert_eq!(s.fault_generation, 1, "fault generation survives restart");

    // Last known progress counters survived.
    let logger = s.tasks.iter().find(|t| t.name == "logger").unwrap();
    assert_eq!(logger.counter, 3);

    // Even after restart, ordinary heartbeats cannot clear safe mode.
    hb(&mut wd, "logger", 999);
    assert!(matches!(wd.feed(), Err(watchdog_host::core::FeedError::SafeMode { .. })));
    assert!(s.safe_mode);

    // The stale-clear rule still holds with the persisted generation.
    assert!(matches!(
        wd.clear_safe_mode(0),
        Err(watchdog_host::core::ClearError::StaleGeneration { current: 1 })
    ));
    wd.clear_safe_mode(1).unwrap();

    // Reset journal survived the restart as well.
    assert_eq!(wd.reset_log().unwrap().len(), 3);
}

#[test]
fn partial_progress_does_not_feed() {
    let (mut wd, _clock) = make();
    hb(&mut wd, "control-loop", 1);
    hb(&mut wd, "sensor-fusion", 1);
    // logger silent entirely.
    match wd.feed() {
        Err(watchdog_host::core::FeedError::Stalled { stalled }) => {
            assert_eq!(stalled, vec!["logger".to_string()]);
        }
        other => panic!("expected single stalled task, got {other:?}"),
    }
}
