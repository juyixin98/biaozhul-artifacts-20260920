//! End-to-end HTTP tests driving the real Axum router against a file-backed
//! SQLite database in manual-clock mode.

use std::sync::Arc;
use tower::ServiceExt;
use watchdog_host::api;
use watchdog_host::clock::ManualClock;
use watchdog_host::{App, ClockMode, Config};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};

fn config() -> Config {
    Config {
        tasks: vec![
            "control-loop".into(),
            "sensor-fusion".into(),
            "logger".into(),
        ],
        window_ms: 1000,
        reset_threshold: 3,
    }
}

async fn call(
    app: &Arc<App>,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(path);
    let body = match body {
        Some(v) => {
            req = req.header("content-type", "application/json");
            Body::from(v.to_string())
        }
        None => Body::empty(),
    };
    let resp = api::router(app.clone())
        .oneshot(req.body(body).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, value)
}

async fn new_app() -> (Arc<App>, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("api-wd.db");
    let clock = Arc::new(ManualClock::new(0));
    let app = App::open(
        db.to_str().unwrap(),
        config(),
        clock.clone(),
        Some(clock),
        ClockMode::Manual,
    )
    .unwrap();
    (app, dir)
}

async fn progress_all(app: &Arc<App>) {
    for (i, name) in ["control-loop", "sensor-fusion", "logger"].iter().enumerate() {
        let (s, v) = call(
            app,
            "POST",
            &format!("/tasks/{name}/heartbeat"),
            Some(json!({ "counter": i + 1 })),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["result"]["progressed"], true);
    }
}

#[tokio::test]
async fn health_and_initial_status() {
    let (app, _d) = new_app().await;
    let (s, v) = call(&app, "GET", "/healthz", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["ok"], true);

    let (s, v) = call(&app, "GET", "/status", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["state"]["safe_mode"], false);
    assert_eq!(v["state"]["fault_generation"], 0);
    assert_eq!(v["state"]["tasks"].as_array().unwrap().len(), 3);
    assert_eq!(v["clock_mode"], "manual");
}

#[tokio::test]
async fn heartbeat_repeat_then_feed_rejected() {
    let (app, _d) = new_app().await;
    progress_all(&app).await;
    let (s, v) = call(&app, "POST", "/feed", None).await;
    assert_eq!(s, StatusCode::OK, "{v}");

    // Same counters again -> heartbeats accepted but flagged as no progress.
    for (i, name) in ["control-loop", "sensor-fusion", "logger"].iter().enumerate() {
        let (s, v) = call(
            &app,
            "POST",
            &format!("/tasks/{name}/heartbeat"),
            Some(json!({ "counter": i + 1 })),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["result"]["progressed"], false);
    }
    let (s, v) = call(&app, "POST", "/feed", None).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"], "stalled");
    assert!(v["message"].as_str().unwrap().contains("no fresh progress"));
}

#[tokio::test]
async fn full_safe_mode_lifecycle_over_http() {
    let (app, _d) = new_app().await;

    // Two tasks work, logger stays stuck: three expired windows.
    for window in 0..3u64 {
        call(
            &app,
            "POST",
            "/tasks/control-loop/heartbeat",
            Some(json!({ "counter": 10 + window })),
        )
        .await;
        call(
            &app,
            "POST",
            "/tasks/sensor-fusion/heartbeat",
            Some(json!({ "counter": 20 + window })),
        )
        .await;
        // A repeated heartbeat from the stuck task changes nothing.
        call(
            &app,
            "POST",
            "/tasks/logger/heartbeat",
            Some(json!({ "counter": 0 })),
        )
        .await;

        let (s, v) = call(
            &app,
            "POST",
            "/clock/advance",
            Some(json!({ "delta_ms": 1001 })),
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert!(v["now_ms"].as_u64().unwrap() >= 1001);

        let (s, v) = call(&app, "POST", "/tick", None).await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["outcome"]["outcome"], "reset");
    }

    let (s, v) = call(&app, "GET", "/status", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["state"]["safe_mode"], true);
    assert_eq!(v["state"]["fault_generation"], 1);
    assert_eq!(v["state"]["consecutive_resets"], 3);

    // Even with all tasks now making real progress, feeds stay rejected.
    for (i, name) in ["control-loop", "sensor-fusion", "logger"].iter().enumerate() {
        let (s, v) = call(
            &app,
            "POST",
            &format!("/tasks/{name}/heartbeat"),
            Some(json!({ "counter": 100u64 + i as u64 })),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["result"]["progressed"], true);
    }
    let (s, v) = call(&app, "POST", "/feed", None).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"], "safe_mode");

    // Tick in safe mode does nothing destructive.
    let (s, _v) = call(
        &app,
        "POST",
        "/clock/advance",
        Some(json!({ "delta_ms": 5000 })),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (s, v) = call(&app, "POST", "/tick", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["outcome"]["outcome"], "safe_mode");
    assert_eq!(v["entered_safe_mode"], false);

    // Stale generation clear rejected.
    let (s, v) = call(
        &app,
        "POST",
        "/safe-mode/clear",
        Some(json!({ "fault_generation": 0 })),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"], "stale_generation");

    // Correct generation clears.
    let (s, v) = call(
        &app,
        "POST",
        "/safe-mode/clear",
        Some(json!({ "fault_generation": 1 })),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["cleared"], true);

    let (s, v) = call(&app, "GET", "/status", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["state"]["safe_mode"], false);

    // Journal retains reset reasons plus last-progress snapshots.
    let (s, v) = call(&app, "GET", "/resets", None).await;
    assert_eq!(s, StatusCode::OK);
    let resets = v["resets"].as_array().unwrap();
    assert_eq!(resets.len(), 3);
    assert!(resets[0]["reason"].as_str().unwrap().contains("logger"));
    let snap: Value = serde_json::from_str(resets[2]["task_snapshot"].as_str().unwrap()).unwrap();
    let logger = snap
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "logger")
        .unwrap();
    assert_eq!(logger["counter"], 0);
}

#[tokio::test]
async fn clock_regression_endpoint_and_unknown_task() {
    let (app, _d) = new_app().await;
    // Advance then rewind via the clock control endpoint. The state machine
    // must observe the forward value first for the regression to be visible.
    call(&app, "POST", "/clock/advance", Some(json!({"delta_ms": 500}))).await;
    call(&app, "GET", "/status", None).await;
    let (s, v) = call(&app, "POST", "/clock/set", Some(json!({"now_ms": 42}))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["now_ms"], 42);

    let (s, v) = call(&app, "POST", "/tick", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["outcome"]["outcome"], "within_window");

    let (s, v) = call(&app, "GET", "/status", None).await;
    assert_eq!(s, StatusCode::OK);
    assert!(v["state"]["clock_anomalies"].as_u64().unwrap() >= 1);

    // Unknown task -> 400; counter regression -> 409.
    let (s, _v) = call(
        &app,
        "POST",
        "/tasks/ghost/heartbeat",
        Some(json!({ "counter": 1 })),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    call(&app, "POST", "/tasks/logger/heartbeat", Some(json!({"counter": 9}))).await;
    let (s, v) = call(
        &app,
        "POST",
        "/tasks/logger/heartbeat",
        Some(json!({ "counter": 8 })),
    )
    .await;
    assert_eq!(s, StatusCode::CONFLICT, "{v}");
    assert_eq!(v["error"], "counter_regression");
}
