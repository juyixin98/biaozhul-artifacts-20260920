//! End-to-end state-machine tests through the Axum router, driven by a
//! deterministic clock and an in-memory (or file-backed) SQLite database.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use std::sync::Arc;
use tower::ServiceExt;
use watchdog_host::clock::ManualClock;
use watchdog_host::db;
use watchdog_host::http::{build_router, AppState};
use watchdog_host::service::WatchdogService;

type HmacSha256 = Hmac<Sha256>;

fn make_app(start_ms: i64) -> (axum::Router, Arc<ManualClock>) {
    make_app_on(":memory:", start_ms)
}

fn make_app_on(path: &str, start_ms: i64) -> (axum::Router, Arc<ManualClock>) {
    let clock = Arc::new(ManualClock::new(start_ms));
    let conn = db::open(path).unwrap();
    let svc = Arc::new(WatchdogService::new(conn, clock.clone() as Arc<_>));
    let app = build_router(AppState {
        svc,
        manual_clock: Some(clock.clone()),
    });
    (app, clock)
}

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let body = match body {
        Some(v) => Body::from(v.to_string()),
        None => Body::empty(),
    };
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(body)
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, value)
}

async fn create_device(app: &axum::Router, id: &str, window_ms: i64, threshold: i64) {
    let (s, _) = call(
        app,
        "POST",
        "/devices",
        Some(json!({
            "device_id": id,
            "window_ms": window_ms,
            "reset_threshold": threshold,
            "operator_key": "test-operator-key",
        })),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
}

async fn register(app: &axum::Router, device: &str, task: &str, initial: Option<u32>) {
    let body = match initial {
        Some(c) => json!({ "task_id": task, "initial_counter": c }),
        None => json!({ "task_id": task }),
    };
    let (s, _) = call(app, "POST", &format!("/devices/{device}/tasks"), Some(body)).await;
    assert_eq!(s, StatusCode::OK);
}

async fn advance_clock(app: &axum::Router, ms: i64) {
    let (s, v) = call(
        app,
        "POST",
        "/admin/clock/advance",
        Some(json!({ "ms": ms })),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
}

async fn set_clock(app: &axum::Router, now_ms: i64) {
    let (s, v) = call(app, "POST", "/admin/clock/set", Some(json!({ "now_ms": now_ms }))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
}

fn sign(key: &str, generation: i64, challenge: &str) -> String {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(key.as_bytes()).unwrap();
    Mac::update(&mut mac, format!("{generation}:{challenge}").as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

const KEY: &str = "test-operator-key";

#[tokio::test]
async fn feed_requires_every_task_to_advance() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    register(&app, "dev", "storage", None).await;

    // Only one of two tasks reports: no feed.
    let (s, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [ { "task_id": "net", "counter": 10 } ] })),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["fed"], false);

    // Both advance: fed.
    let (s, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [
            { "task_id": "net", "counter": 11 },
            { "task_id": "storage", "counter": 7 }
        ]})),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["fed"], true);
    assert_eq!(v["consecutive_resets"], 0);
}

#[tokio::test]
async fn repeated_heartbeat_is_not_progress() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    register(&app, "dev", "storage", None).await;

    let body = Some(json!({ "reports": [
        { "task_id": "net", "counter": 11 },
        { "task_id": "storage", "counter": 7 }
    ]}));
    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", body.clone()).await;
    assert_eq!(v["fed"], true);

    // Same counters, any number of times: alive but stuck, never fed again.
    for _ in 0..5 {
        let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", body.clone()).await;
        assert_eq!(v["fed"], false, "identical counters must not feed");
        assert_eq!(v["safe_mode"], false);
    }
    let (_, st) = call(&app, "GET", "/devices/dev", None).await;
    assert_eq!(st["total_feeds"], 1);
}

#[tokio::test]
async fn stuck_task_reaches_safe_mode_and_heartbeats_cannot_clear_it() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    register(&app, "dev", "storage", None).await;

    // Three missed windows -> 3 consecutive resets -> safe mode (generation 1).
    for _ in 0..3 {
        advance_clock(&app, 1000).await;
        let (s, v) = call(&app, "POST", "/devices/dev/tick", None).await;
        assert_eq!(s, StatusCode::OK, "{v}");
    }
    let (_, st) = call(&app, "GET", "/devices/dev", None).await;
    assert_eq!(st["status"], "safe_mode");
    assert_eq!(st["consecutive_resets"], 3);
    assert_eq!(st["fault_generation"], 1);

    // Ordinary heartbeats — even with fresh-looking progress — never clear it.
    for n in 1..5 {
        let (_, v) = call(
            &app,
            "POST",
            "/devices/dev/heartbeat",
            Some(json!({ "reports": [
                { "task_id": "net", "counter": 100 + n },
                { "task_id": "storage", "counter": 200 + n }
            ]})),
        )
        .await;
        assert_eq!(v["fed"], false);
        assert_eq!(v["safe_mode"], true);
    }
    let (_, st) = call(&app, "GET", "/devices/dev", None).await;
    assert_eq!(st["status"], "safe_mode");

    // Reset reasons are retained for forensics.
    let (_, resets) = call(&app, "GET", "/devices/dev/resets", None).await;
    assert_eq!(resets["resets"].as_array().unwrap().len(), 3);
    assert_eq!(
        resets["resets"][2]["reason"],
        "watchdog window expired without all tasks advancing"
    );
    assert_eq!(resets["resets"][2]["fault_generation_after"], 1);
}

#[tokio::test]
async fn brief_jitter_resets_the_consecutive_streak_on_successful_feed() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    register(&app, "dev", "storage", None).await;

    // First window missed: streak = 1.
    advance_clock(&app, 1000).await;
    call(&app, "POST", "/devices/dev/tick", None).await;

    // Device recovers and feeds: streak returns to 0.
    let (_, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [
            { "task_id": "net", "counter": 1 },
            { "task_id": "storage", "counter": 1 }
        ]})),
    )
    .await;
    assert_eq!(v["fed"], true);
    assert_eq!(v["consecutive_resets"], 0);

    // A later isolated timeout starts a NEW streak (1, not 2): no safe mode.
    advance_clock(&app, 1000).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["consecutive_resets"], 1);
    assert_eq!(v["status"], "running");
}

#[tokio::test]
async fn stale_clearance_rejected_current_generation_accepted() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 1).await;
    register(&app, "dev", "net", None).await;
    advance_clock(&app, 1000).await;
    call(&app, "POST", "/devices/dev/tick", None).await;
    let (_, st) = call(&app, "GET", "/devices/dev", None).await;
    assert_eq!(st["status"], "safe_mode");
    assert_eq!(st["fault_generation"], 1);

    // A challenge for the current fault generation is issued.
    let (s, ch) = call(&app, "POST", "/devices/dev/safe-mode/challenge", None).await;
    assert_eq!(s, StatusCode::OK);
    let nonce = ch["challenge"].as_str().unwrap().to_string();
    assert_eq!(ch["generation"], 1);

    // Old generation signature (e.g. signed before the latest fault): rejected.
    let stale = json!({
        "generation": 0,
        "challenge": nonce,
        "hmac_hex": sign(KEY, 0, &nonce),
    });
    let (s, v) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(stale)).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{v}");
    assert!(v["error"].as_str().unwrap().contains("stale"));

    // Current generation but wrong key: rejected.
    let forged = json!({
        "generation": 1,
        "challenge": nonce,
        "hmac_hex": sign("wrong-operator-key", 1, &nonce),
    });
    let (s, v) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(forged)).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{v}");
    assert!(v["error"].as_str().unwrap().contains("HMAC"));

    // Correct signature for the CURRENT generation: accepted.
    let ok = json!({
        "generation": 1,
        "challenge": nonce,
        "hmac_hex": sign(KEY, 1, &nonce),
    });
    let (s, v) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(ok)).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["cleared"], true);
    let (_, st) = call(&app, "GET", "/devices/dev", None).await;
    assert_eq!(st["status"], "running");
    assert_eq!(st["consecutive_resets"], 0);

    // The nonce was single-use: replaying the same request cannot re-clear.
    let replay = json!({
        "generation": 1,
        "challenge": nonce,
        "hmac_hex": sign(KEY, 1, &nonce),
    });
    let (s, _) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(replay)).await;
    assert_eq!(s, StatusCode::CONFLICT);
}

#[tokio::test]
async fn clock_rollback_causes_no_spurious_reset() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    let hb = |c: u32| {
        Some(json!({ "reports": [ { "task_id": "net", "counter": c } ] }))
    };

    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", hb(1)).await;
    assert_eq!(v["fed"], true);

    advance_clock(&app, 900).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], false);

    // NTP-style rollback, well behind the window start.
    set_clock(&app, 995_900).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], false, "backward clock must not time out");

    // Progress while rolled back still feeds (window effectively extended).
    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", hb(2)).await;
    assert_eq!(v["fed"], true);

    // Timeout only fires once real forward elapsed time crosses the window.
    set_clock(&app, 1_001_001).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], true);
    assert_eq!(v["consecutive_resets"], 1);
}

#[tokio::test]
async fn clock_wraparound_like_32bit_tick_counter() {
    // Simulate a firmware tick base near u32::MAX milliseconds that wraps.
    let start: i64 = (u32::MAX as i64) - 200;
    let (app, _c) = make_app(start);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;

    let (_, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [ { "task_id": "net", "counter": 1 } ] })),
    )
    .await;
    assert_eq!(v["fed"], true);

    // Clock wraps to small values: no false timeout.
    set_clock(&app, 200).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], false);

    let (_, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [ { "task_id": "net", "counter": 2 } ] })),
    )
    .await;
    assert_eq!(v["fed"], true);

    set_clock(&app, 1300).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], true);
}

#[tokio::test]
async fn large_forward_jump_applies_one_reset_only() {
    // Policy: an evaluation that misses many windows records one reset and
    // re-arms from "now" (the device itself would have rebooted only once).
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", None).await;
    set_clock(&app, 1_000_000 + 10 * 1000).await;
    let (_, v) = call(&app, "POST", "/devices/dev/tick", None).await;
    assert_eq!(v["reset_applied"], true);
    assert_eq!(v["consecutive_resets"], 1);
}

#[tokio::test]
async fn progress_counter_wraparound_is_real_progress() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 3).await;
    register(&app, "dev", "net", Some(u32::MAX - 1)).await;

    let hb = |c: u32| {
        Some(json!({ "reports": [ { "task_id": "net", "counter": c } ] }))
    };
    // MAX-1 -> MAX: advance.
    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", hb(u32::MAX)).await;
    assert_eq!(v["fed"], true);
    // MAX -> 0: one tick of advance across the wrap boundary.
    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", hb(0)).await;
    assert_eq!(v["fed"], true);
    // 0 -> 0: repeat, NOT progress.
    let (_, v) = call(&app, "POST", "/devices/dev/heartbeat", hb(0)).await;
    assert_eq!(v["fed"], false);
}

#[tokio::test]
async fn resets_and_boot_count_persist_across_process_restart() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("wd.db");
    let db_path = db_path.to_str().unwrap();

    {
        let (app, clock) = make_app_on(db_path, 1_000_000);
        create_device(&app, "dev", 1000, 10).await;
        register(&app, "dev", "net", None).await;
        // Two watchdog timeouts...
        advance_clock(&app, 1000).await;
        call(&app, "POST", "/devices/dev/tick", None).await;
        advance_clock(&app, 1000).await;
        call(&app, "POST", "/devices/dev/tick", None).await;
        // ...then a firmware-reported reset carrying the persistent boot count.
        let (s, v) = call(
            &app,
            "POST",
            "/devices/dev/reset",
            Some(json!({
                "reason": "brownout during flash erase",
                "source": "brownout_detector",
                "boot_count": 5,
            })),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["consecutive_resets"], 3);
        assert_eq!(v["boot_count"], 5);
        drop(app);
        drop(clock);
    }

    // "Reboot" the host process: reopen the same database file.
    let now = 1_000_000 + 3_000;
    {
        let (app, _c) = make_app_on(db_path, now + 500);
        let (_, st) = call(&app, "GET", "/devices/dev", None).await;
        assert_eq!(st["status"], "running");
        assert_eq!(st["consecutive_resets"], 3);
        assert_eq!(st["fault_generation"], 0);
        assert_eq!(st["boot_count"], 5);
        let (_, resets) = call(&app, "GET", "/devices/dev/resets", None).await;
        assert_eq!(resets["resets"].as_array().unwrap().len(), 3);
        assert_eq!(resets["resets"][2]["source"], "brownout_detector");

        // Seven more consecutive resets (3 + 7 = threshold 10) enter safe mode
        // using the streak restored from disk; advance the clock past each
        // re-armed window before every tick.
        for _ in 0..7 {
            advance_clock(&app, 1000).await;
            call(&app, "POST", "/devices/dev/tick", None).await;
        }
        let (_, st) = call(&app, "GET", "/devices/dev", None).await;
        assert_eq!(st["status"], "safe_mode");
        assert_eq!(st["consecutive_resets"], 10);
        assert_eq!(st["fault_generation"], 1);
    }

    // A second host restart still shows the persisted safe mode / generation.
    {
        let (app, _c) = make_app_on(db_path, 1_020_000);
        let (_, st) = call(&app, "GET", "/devices/dev", None).await;
        assert_eq!(st["status"], "safe_mode");
        assert_eq!(st["fault_generation"], 1);
        assert_eq!(st["total_resets"], 10);
        // Heartbeats after restart still cannot clear safe mode.
        let (_, v) = call(
            &app,
            "POST",
            "/devices/dev/heartbeat",
            Some(json!({ "reports": [ { "task_id": "net", "counter": 999 } ] })),
        )
        .await;
        assert_eq!(v["safe_mode"], true);
        assert_eq!(v["fed"], false);
    }
}

#[tokio::test]
async fn expired_challenge_must_be_reissued() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 1).await;
    register(&app, "dev", "net", None).await;
    advance_clock(&app, 1000).await;
    call(&app, "POST", "/devices/dev/tick", None).await;

    let (_, ch) = call(&app, "POST", "/devices/dev/safe-mode/challenge", None).await;
    let old_nonce = ch["challenge"].as_str().unwrap().to_string();
    // TTL is 60 s; outlive it.
    advance_clock(&app, 60_001).await;
    let expired = json!({
        "generation": 1,
        "challenge": old_nonce,
        "hmac_hex": sign(KEY, 1, &old_nonce),
    });
    let (s, v) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(expired)).await;
    assert_eq!(s, StatusCode::FORBIDDEN, "{v}");
    assert!(v["error"].as_str().unwrap().contains("expired"));

    let (_, ch) = call(&app, "POST", "/devices/dev/safe-mode/challenge", None).await;
    let new_nonce = ch["challenge"].as_str().unwrap().to_string();
    let ok = json!({
        "generation": 1,
        "challenge": new_nonce,
        "hmac_hex": sign(KEY, 1, &new_nonce),
    });
    let (s, v) = call(&app, "POST", "/devices/dev/safe-mode/clear", Some(ok)).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["cleared"], true);
}

#[tokio::test]
async fn resets_reported_in_safe_mode_are_recorded_but_not_counted() {
    let (app, _c) = make_app(1_000_000);
    create_device(&app, "dev", 1000, 1).await;
    register(&app, "dev", "net", None).await;
    advance_clock(&app, 1000).await;
    call(&app, "POST", "/devices/dev/tick", None).await;

    let (s, v) = call(
        &app,
        "POST",
        "/devices/dev/reset",
        Some(json!({ "reason": "firmware panic", "source": "panic", "boot_count": 2 })),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["status"], "safe_mode");
    assert_eq!(v["consecutive_resets"], 1); // unchanged
    assert_eq!(v["fault_generation"], 1);
    assert_eq!(v["boot_count"], 2);

    let (_, resets) = call(&app, "GET", "/devices/dev/resets", None).await;
    let list = resets["resets"].as_array().unwrap();
    assert_eq!(list.len(), 2);
    assert_eq!(list[1]["counted"], false);
    assert_eq!(list[1]["consecutive_after"], 1);
}

#[tokio::test]
async fn input_validation() {
    let (app, _c) = make_app(1_000_000);

    // Duplicate device.
    create_device(&app, "dev", 1000, 3).await;
    let (s, _) = call(
        &app,
        "POST",
        "/devices",
        Some(json!({ "device_id": "dev" })),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);

    // Unknown device / task.
    let (s, _) = call(&app, "GET", "/devices/nope", None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    register(&app, "dev", "net", None).await;
    let (s, v) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [ { "task_id": "ghost", "counter": 1 } ] })),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{v}");

    // Duplicate report in one heartbeat.
    let (s, _) = call(
        &app,
        "POST",
        "/devices/dev/heartbeat",
        Some(json!({ "reports": [
            { "task_id": "net", "counter": 1 },
            { "task_id": "net", "counter": 2 }
        ]})),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}
