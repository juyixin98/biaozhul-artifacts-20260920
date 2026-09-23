//! End-to-end acceptance over the real HTTP entry point:
//! two writers started on the same snapshot are interleaved with a long
//! reader; the first commit wins, the second gets 409; GC while the reader is
//! pinned cannot remove its history; after release the space is reclaimed.

mod common;

use std::sync::Arc;

use mvcc_reclaim::http::Server;
use mvcc_reclaim::io::{FaultIo, FaultKind, FaultRule};
use mvcc_reclaim::mvcc::Engine;

fn start(dir: &std::path::Path) -> String {
    let server = Server::new(dir).unwrap();
    let addr = server.serve("127.0.0.1:0").unwrap();
    // Keep the server alive for the whole test.
    std::mem::forget(server);
    format!("127.0.0.1:{}", addr.port())
}

fn jget(addr: &str, path: &str) -> common::HttpResponse {
    common::http(addr, "GET", path, "")
}
fn jpost(addr: &str, path: &str, body: &str) -> common::HttpResponse {
    common::http(addr, "POST", path, body)
}
fn jput(addr: &str, path: &str, body: &str) -> common::HttpResponse {
    common::http(addr, "PUT", path, body)
}

#[test]
fn http_writers_conflict_long_reader_and_gc() {
    let dir = common::temp_dir("http-acceptance");
    let addr = start(&dir);

    // Seed v1.
    let r = jpost(&addr, "/tx/write", "{}");
    assert_eq!(r.status, 200);
    let w0 = common::int(&r.body, "write_txn_id");
    let r = jput(
        &addr,
        &format!("/tx/write/{w0}"),
        r#"{"ops":[{"put":{"k":"x","v":"v1"}},{"put":{"k":"y","v":"y1"}}]}"#,
    );
    assert_eq!(r.status, 200);
    assert_eq!(common::int(&r.body, "buffered"), 2);
    let r = jpost(&addr, &format!("/tx/write/{w0}/commit"), "{}");
    assert_eq!(r.status, 200);
    assert_eq!(common::int(&r.body, "version"), 1);

    // Long reader pins v1.
    let r = jpost(&addr, "/tx/read", "{}");
    assert_eq!(r.status, 200);
    assert_eq!(common::int(&r.body, "version"), 1);
    let reader = common::int(&r.body, "read_txn_id");

    // Reader sees v1 content.
    let r = jget(&addr, &format!("/tx/read/{reader}/get?key=x"));
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("v1"));

    // Writer A and B both snapshot v1.
    let wa = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    let wb = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    let r = jput(
        &addr,
        &format!("/tx/write/{wa}"),
        r#"{"ops":[{"put":{"k":"x","v":"A"}}]}"#,
    );
    assert_eq!(r.status, 200);
    let r = jput(
        &addr,
        &format!("/tx/write/{wb}"),
        r#"{"ops":[{"put":{"k":"x","v":"B"}}]}"#,
    );
    assert_eq!(r.status, 200);

    // A commits (v2).
    let r = jpost(&addr, &format!("/tx/write/{wa}/commit"), "{}");
    assert_eq!(r.status, 200);
    assert_eq!(common::int(&r.body, "version"), 2);

    // B commits -> 409 conflict, names x.
    let r = jpost(&addr, &format!("/tx/write/{wb}/commit"), "{}");
    assert_eq!(r.status, 409);
    assert_eq!(r.body.get("error").and_then(|v| v.as_str()), Some("write-write conflict"));
    let keys = match r.body.get("keys") {
        Some(mvcc_reclaim::json::Json::Arr(a)) => a
            .iter()
            .map(|j| j.as_str().unwrap().to_string())
            .collect::<Vec<_>>(),
        other => panic!("expected keys array, got {other:?}"),
    };
    assert_eq!(keys, vec!["x"]);

    // Latest state reflects A only.
    let r = jget(&addr, "/get?key=x");
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("A"));

    // The long reader STILL sees v1.
    let r = jget(&addr, &format!("/tx/read/{reader}/get?key=x"));
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("v1"));
    let r = jget(&addr, &format!("/tx/read/{reader}/get?key=y"));
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("y1"));

    // Create more history to have something reclaimable once the reader goes.
    for i in 0..5 {
        let wid = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
        let body = format!(r#"{{"ops":[{{"put":{{"k":"x","v":"n{i}"}}}}]}}"#);
        assert_eq!(jput(&addr, &format!("/tx/write/{wid}"), &body).status, 200);
        assert_eq!(
            jpost(&addr, &format!("/tx/write/{wid}/commit"), "{}").status,
            200
        );
    }

    // Stats while the reader is active list it and pin watermark to 1.
    let st = jget(&addr, "/stats");
    assert_eq!(common::int(&st.body, "watermark"), 1);
    let snaps = st.body.get("snapshots").unwrap();
    let snap_count = match snaps {
        mvcc_reclaim::json::Json::Arr(a) => a.len(),
        _ => panic!("snapshots not array"),
    };
    assert!(snap_count >= 1);

    // GC with active reader must not break the reader (watermark 1 keeps all
    // versions >= 1 — reader stays valid).
    let r = jpost(&addr, "/gc", "{}");
    assert_eq!(r.status, 200);
    let r = jget(&addr, &format!("/tx/read/{reader}/get?key=x"));
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("v1"));

    // Release the reader; GC can now reclaim old history.
    assert_eq!(
        jpost(&addr, &format!("/tx/read/{reader}/release"), "{}").status,
        200
    );
    let st = jget(&addr, "/stats");
    let before = common::int(&st.body, "file_bytes");
    let reclaimable = common::int(&st.body, "reclaimable_bytes");
    assert!(reclaimable > 0, "expected reclaimable bytes, got {reclaimable}");

    let r = jpost(&addr, "/gc", "{}");
    assert_eq!(r.status, 200);
    let ran = match r.body.get("ran") {
        Some(mvcc_reclaim::json::Json::Bool(b)) => *b,
        other => panic!("ran not bool: {other:?}"),
    };
    assert!(ran, "GC should reclaim old history");
    assert!(common::int(&r.body, "reclaimed") > 0);
    assert!(common::int(&r.body, "bytes_after") < before);

    // Latest read still correct after compaction.
    let r = jget(&addr, "/get?key=x");
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("n4"));

    // New commits continue on the compacted log.
    let wid = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    let r = jput(
        &addr,
        &format!("/tx/write/{wid}"),
        r#"{"ops":[{"put":{"k":"x","v":"after"}}]}"#,
    );
    assert_eq!(r.status, 200);
    let r = jpost(&addr, &format!("/tx/write/{wid}/commit"), "{}");
    assert_eq!(r.status, 200);
    let r = jget(&addr, "/get?key=x");
    assert_eq!(r.body.get("value").and_then(|v| v.as_str()), Some("after"));

    // Abort path leaves nothing behind.
    let wid = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    assert_eq!(
        jpost(&addr, &format!("/tx/write/{wid}/abort"), "{}").status,
        200
    );
    assert_eq!(
        jpost(&addr, &format!("/tx/write/{wid}/commit"), "{}").status,
        404
    );

    // Unknown routes / bad bodies.
    assert_eq!(jget(&addr, "/nope").status, 404);
    let wid = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    assert_eq!(
        jput(&addr, &format!("/tx/write/{wid}"), "not json").status,
        400
    );
}

#[test]
fn http_io_failure_returns_500_and_server_survives() {
    let dir = common::temp_dir("http-fault");

    // Fail the sync on the first commit publication (sync #1 is header; #2 is
    // the v1 CMMT). The HTTP layer must translate this to 500, not a crash.
    // Stack SimCrashIo underneath so a reopen after the failed fsync sees the
    // bytes lost (as real power loss would behave).
    let crash = Arc::new(mvcc_reclaim::io::SimCrashIo::new(Arc::new(mvcc_reclaim::io::StdIo)));
    let mut rule = FaultRule::new(FaultKind::Sync, 2);
    rule.times = 1;
    let rules = vec![rule];
    let fault_engine = mvcc_reclaim::mvcc::Engine::open_with_io(
        Arc::new(FaultIo::new(crash.clone() as Arc<dyn mvcc_reclaim::io::Io>, rules)),
        &dir,
    )
    .unwrap();
    let server = Server::with_engine(fault_engine);
    let addr = server.serve("127.0.0.1:0").unwrap();
    std::mem::forget(server);
    let addr = format!("127.0.0.1:{}", addr.port());

    assert_eq!(jget(&addr, "/stats").status, 200);

    let wid = common::int(&jpost(&addr, "/tx/write", "{}").body, "write_txn_id");
    let r = jput(
        &addr,
        &format!("/tx/write/{wid}"),
        r#"{"ops":[{"put":{"k":"z","v":"1"}}]}"#,
    );
    assert_eq!(r.status, 200);
    let r = jpost(&addr, &format!("/tx/write/{wid}/commit"), "{}");
    assert_eq!(r.status, 500);

    // Server survived: stats still answers.
    assert_eq!(jget(&addr, "/stats").status, 200);

    // Simulate the power loss the failed fsync represents, then reopen clean.
    crash.crash();
    let clean = Engine::open(&dir).unwrap();
    assert_eq!(clean.latest_version(), 0);
    assert_eq!(clean.get_latest(b"z"), None);
}
