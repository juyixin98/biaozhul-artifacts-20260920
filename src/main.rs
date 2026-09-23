//! HTTP layer for the MVCC key-value service (axum 0.8).

mod db;

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use db::{Db, GcReport, MvccError};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

type AppState = Arc<Db>;

#[tokio::main]
async fn main() {
    let addr: SocketAddr = std::env::var("MVCC_ADDR")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| SocketAddr::from(([127, 0, 0, 1], 3000)));

    let state = Arc::new(Db::new());
    let app = router(state);

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind listener");
    eprintln!("mvcc-kv listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}

fn router(state: AppState) -> Router {
    Router::new()
        .route("/", get(index))
        // transactions
        .route("/txn/begin", post(begin))
        .route("/txn/{id}/commit", post(commit))
        .route("/txn/{id}/abort", post(abort))
        .route("/txn/{id}/status", get(txn_status))
        .route(
            "/txn/{id}/kv/{key}",
            get(txn_get).put(txn_put).delete(txn_delete),
        )
        // read-only snapshots
        .route("/snapshot/open", post(snapshot_open))
        .route("/snapshot/{id}/close", post(snapshot_close))
        .route("/snapshot/{id}/status", get(snapshot_status))
        .route("/snapshot/{id}/kv/{key}", get(snapshot_get))
        // garbage collection
        .route("/gc", post(gc))
        // debug
        .route("/stats", get(stats))
        .route("/debug/versions/{key}", get(key_versions))
        // convenience auto-commit endpoints
        .route("/kv/{key}", get(auto_get).put(auto_put).delete(auto_delete))
        .with_state(state)
}

async fn index() -> impl IntoResponse {
    Json(json!({
        "service": "mvcc-kv",
        "description": "single-process in-memory MVCC key-value store: snapshot reads, first-committer-wins commits, tombstones, version GC",
        "endpoints": {
            "POST /txn/begin": "start a transaction -> {txn_id, start_ts}",
            "POST /txn/:id/commit": "commit (409 on write-write conflict)",
            "POST /txn/:id/abort": "abort and discard buffered writes",
            "GET  /txn/:id/status": "transaction info",
            "GET  /txn/:id/kv/:key": "snapshot read inside a transaction (read-your-writes)",
            "PUT  /txn/:id/kv/:key": "buffer a write (body is the JSON value)",
            "DELETE /txn/:id/kv/:key": "buffer a delete (tombstone at commit)",
            "POST /snapshot/open": "open a read-only snapshot, body {\"ts\": <optional>}",
            "POST /snapshot/:id/close": "close snapshot (pinned versions become GC-able)",
            "GET  /snapshot/:id/status": "snapshot info",
            "GET  /snapshot/:id/kv/:key": "read a key as of the snapshot timestamp",
            "POST /gc": "reclaim versions older than the reader watermark",
            "GET  /stats": "timestamps, live readers, version counts, watermark",
            "GET  /debug/versions/:key": "full committed version chain of a key",
            "GET  /kv/:key": "autocommit: read latest committed value (?as_of=TS for a one-shot snapshot read)",
            "PUT  /kv/:key": "autocommit: write in a fresh one-shot transaction",
            "DELETE /kv/:key": "autocommit: delete in a fresh one-shot transaction"
        }
    }))
}

// ---------- transactions ----------

#[derive(Serialize)]
struct BeginOut {
    txn_id: u64,
    start_ts: u64,
}

async fn begin(State(db): State<AppState>) -> impl IntoResponse {
    let (id, start_ts) = db.begin();
    Json(BeginOut {
        txn_id: id,
        start_ts,
    })
}

#[derive(Serialize)]
struct CommitOut {
    txn_id: u64,
    committed: bool,
    commit_ts: u64,
}

async fn commit(State(db): State<AppState>, Path(id): Path<u64>) -> Response {
    match db.commit(id) {
        Ok(ts) => Json(CommitOut {
            txn_id: id,
            committed: true,
            commit_ts: ts,
        })
        .into_response(),
        Err(e) => map_err(e),
    }
}

async fn abort(State(db): State<AppState>, Path(id): Path<u64>) -> Response {
    match db.abort(id) {
        Ok(()) => Json(json!({"txn_id": id, "aborted": true})).into_response(),
        Err(e) => map_err(e),
    }
}

async fn txn_status(State(db): State<AppState>, Path(id): Path<u64>) -> Response {
    match db.txn_status(id) {
        Ok(s) => Json(s).into_response(),
        Err(e) => map_err(e),
    }
}

#[derive(Serialize)]
struct ValueOut {
    key: String,
    value: Option<Value>,
    found: bool,
}

async fn txn_get(State(db): State<AppState>, Path((id, key)): Path<(u64, String)>) -> Response {
    match db.get(id, &key) {
        Ok(value) => Json(ValueOut {
            found: value.is_some(),
            key,
            value,
        })
        .into_response(),
        Err(e) => map_err(e),
    }
}

async fn txn_put(
    State(db): State<AppState>,
    Path((id, key)): Path<(u64, String)>,
    body: String,
) -> Response {
    let value = match parse_json_body(&body) {
        Ok(v) => v,
        Err(r) => return r,
    };
    match db.put(id, key.clone(), value) {
        Ok(()) => Json(json!({"txn_id": id, "key": key, "buffered": "put"})).into_response(),
        Err(e) => map_err(e),
    }
}

async fn txn_delete(State(db): State<AppState>, Path((id, key)): Path<(u64, String)>) -> Response {
    match db.delete(id, key.clone()) {
        Ok(()) => Json(json!({"txn_id": id, "key": key, "buffered": "delete"})).into_response(),
        Err(e) => map_err(e),
    }
}

// ---------- snapshots ----------

#[derive(Deserialize)]
struct SnapshotOpenIn {
    ts: Option<u64>,
}

#[derive(Serialize)]
struct SnapshotOpenOut {
    snapshot_id: u64,
    ts: u64,
}

async fn snapshot_open(State(db): State<AppState>, body: Option<Json<SnapshotOpenIn>>) -> Response {
    let ts = body.and_then(|b| b.ts);
    match db.snapshot_open(ts) {
        Ok((id, ts)) => Json(SnapshotOpenOut {
            snapshot_id: id,
            ts,
        })
        .into_response(),
        Err(e) => map_err(e),
    }
}

async fn snapshot_close(State(db): State<AppState>, Path(id): Path<u64>) -> Response {
    match db.snapshot_close(id) {
        Ok(()) => Json(json!({"snapshot_id": id, "closed": true})).into_response(),
        Err(e) => map_err(e),
    }
}

async fn snapshot_status(State(db): State<AppState>, Path(id): Path<u64>) -> Response {
    match db.snapshot_status(id) {
        Ok(s) => Json(s).into_response(),
        Err(e) => map_err(e),
    }
}

async fn snapshot_get(
    State(db): State<AppState>,
    Path((id, key)): Path<(u64, String)>,
) -> Response {
    match db.snapshot_read(id, &key) {
        Ok(value) => Json(ValueOut {
            found: value.is_some(),
            key,
            value,
        })
        .into_response(),
        Err(e) => map_err(e),
    }
}

// ---------- GC ----------

async fn gc(State(db): State<AppState>) -> impl IntoResponse {
    let report: GcReport = db.gc();
    Json(report)
}

// ---------- debug ----------

async fn stats(State(db): State<AppState>) -> impl IntoResponse {
    Json(db.stats())
}

async fn key_versions(State(db): State<AppState>, Path(key): Path<String>) -> impl IntoResponse {
    Json(json!({"key": key, "versions": db.key_versions(&key)}))
}

// ---------- convenience auto-commit endpoints ----------

#[derive(Deserialize)]
struct AsOf {
    as_of: Option<u64>,
}

async fn auto_get(
    State(db): State<AppState>,
    Path(key): Path<String>,
    Query(q): Query<AsOf>,
) -> Response {
    if let Some(ts) = q.as_of {
        match db.snapshot_open(Some(ts)) {
            Ok((sid, _ts)) => {
                let value = db.snapshot_read(sid, &key);
                let _ = db.snapshot_close(sid);
                match value {
                    Ok(value) => Json(ValueOut {
                        found: value.is_some(),
                        key,
                        value,
                    })
                    .into_response(),
                    Err(e) => map_err(e),
                }
            }
            Err(e) => map_err(e),
        }
    } else {
        let (txn_id, _) = db.begin();
        let value = db.get(txn_id, &key);
        let _ = db.commit(txn_id);
        match value {
            Ok(value) => Json(ValueOut {
                found: value.is_some(),
                key,
                value,
            })
            .into_response(),
            Err(e) => map_err(e),
        }
    }
}

async fn auto_put(State(db): State<AppState>, Path(key): Path<String>, body: String) -> Response {
    let value = match parse_json_body(&body) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let (txn_id, _) = db.begin();
    if let Err(e) = db.put(txn_id, key.clone(), value) {
        return map_err(e);
    }
    match db.commit(txn_id) {
        Ok(ts) => Json(json!({"key": key, "committed": true, "commit_ts": ts})).into_response(),
        Err(e) => map_err(e),
    }
}

async fn auto_delete(State(db): State<AppState>, Path(key): Path<String>) -> Response {
    let (txn_id, _) = db.begin();
    if let Err(e) = db.delete(txn_id, key.clone()) {
        return map_err(e);
    }
    match db.commit(txn_id) {
        Ok(ts) => Json(json!({"key": key, "deleted": true, "commit_ts": ts})).into_response(),
        Err(e) => map_err(e),
    }
}

// ---------- errors ----------

/// PUT bodies are raw JSON values; accept an empty body as null so `curl -X
/// PUT` without a body does not fail outright.
fn parse_json_body(body: &str) -> Result<Value, Response> {
    let trimmed = body.trim();
    if trimmed.is_empty() {
        return Ok(Value::Null);
    }
    serde_json::from_str(trimmed).map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": format!("request body must be a valid JSON value: {e}")})),
        )
            .into_response()
    })
}

fn map_err(e: MvccError) -> Response {
    let (status, code) = match &e {
        MvccError::TxnNotFound(_) | MvccError::SnapshotNotFound(_) => {
            (StatusCode::NOT_FOUND, "not_found")
        }
        MvccError::WriteConflict { .. } => (StatusCode::CONFLICT, "write_conflict"),
        MvccError::InvalidSnapshotTs { .. } => (StatusCode::BAD_REQUEST, "invalid_timestamp"),
    };
    (status, Json(json!({"error": e.to_string(), "code": code}))).into_response()
}

// integration tests run against `router` via in-process requests
#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    fn app() -> Router {
        router(Arc::new(Db::new()))
    }

    async fn send(
        app: &Router,
        method: &str,
        uri: &str,
        body: Option<&str>,
    ) -> (StatusCode, Value) {
        let builder = Request::builder().method(method).uri(uri);
        let req = match body {
            Some(b) => builder
                .header("content-type", "application/json")
                .body(Body::from(b.to_owned()))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        let resp = app.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, value)
    }

    async fn begin(app: &Router) -> u64 {
        let (_, v) = send(app, "POST", "/txn/begin", None).await;
        v["txn_id"].as_u64().unwrap()
    }

    async fn snapshot_open_at(app: &Router, ts: u64) -> u64 {
        let (s, v) = send(
            app,
            "POST",
            "/snapshot/open",
            Some(&format!(r#"{{"ts": {ts}}}"#)),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "snapshot open failed: {v}");
        v["snapshot_id"].as_u64().unwrap()
    }

    /// Acceptance scenario: a long reader spans three overwrites and a
    /// delete; its view never changes, concurrent writers commit exactly
    /// one, and reads are identical before and after GC.
    #[tokio::test]
    async fn acceptance_long_reader_overwrites_delete_gc() {
        let app = app();

        // initial value: k=v0 @ ts1
        let (s, v) = send(&app, "PUT", "/kv/k", Some("\"v0\"")).await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["commit_ts"], 1);

        // long reader opens at ts=1 and sees v0
        let long = begin(&app).await;
        let snap1 = snapshot_open_at(&app, 1).await;

        // three overwrites @ ts2,3,4 and a delete @ ts5
        for val in ["v1", "v2", "v3"] {
            let (s, v) = send(&app, "PUT", "/kv/k", Some(&format!("\"{val}\""))).await;
            assert_eq!(s, StatusCode::OK, "{v}");
        }
        let (s, v) = send(&app, "DELETE", "/kv/k", None).await;
        assert_eq!(s, StatusCode::OK, "{v}");
        assert_eq!(v["commit_ts"], 5);

        // the long reader and the ts1 snapshot still see v0
        for (label, uri) in [
            ("long txn", format!("/txn/{long}/kv/k")),
            ("ts1 snapshot", format!("/snapshot/{snap1}/kv/k")),
        ] {
            let (s, v) = send(&app, "GET", &uri, None).await;
            assert_eq!(s, StatusCode::OK);
            assert_eq!(
                v["value"], "v0",
                "{label} must keep seeing v0 across overwrites/delete"
            );
        }

        // a fresh reader sees the key as deleted
        let (_, v) = send(&app, "GET", "/kv/k", None).await;
        assert_eq!(v["found"], false);
        assert_eq!(v["value"], Value::Null);

        // pin: readers at each historical timestamp see their own version
        for ts in 1u64..=5 {
            let (s, v) = send(&app, "GET", &format!("/kv/k?as_of={ts}"), None).await;
            assert_eq!(s, StatusCode::OK);
            match ts {
                1 => assert_eq!(v["value"], "v0"),
                2 => assert_eq!(v["value"], "v1"),
                3 => assert_eq!(v["value"], "v2"),
                4 => assert_eq!(v["value"], "v3"),
                _ => assert_eq!(v["found"], false),
            }
        }

        // the full chain still holds all five versions
        let (s, versions) = send(&app, "GET", "/debug/versions/k", None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(versions["versions"].as_array().unwrap().len(), 5);

        // GC while the ts1 snapshot and long txn are open: watermark = 1,
        // only the newest version at/below 1 is retained, i.e. nothing the
        // old readers depend on is dropped (v0 @ ts1 stays).
        let (s, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(g["watermark"], 1, "old readers pin the watermark");
        assert_eq!(g["versions_removed"], 0);
        let (_, v) = send(&app, "GET", &format!("/snapshot/{snap1}/kv/k"), None).await;
        assert_eq!(v["value"], "v0");
        let (_, v) = send(&app, "GET", &format!("/txn/{long}/kv/k"), None).await;
        assert_eq!(v["value"], "v0");

        // close the ts1 snapshot; long txn still pins watermark 1
        let (s, _) = send(&app, "POST", &format!("/snapshot/{snap1}/close"), None).await;
        assert_eq!(s, StatusCode::OK);
        let (_, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(g["watermark"], 1);
        assert_eq!(g["versions_removed"], 0);

        // open a snapshot at the current latest timestamp (ts=5) *before*
        // reclaiming; it records the "after delete" view that must remain
        // identical across GC.
        let s5 = snapshot_open_at(&app, 5).await;
        let (_, v) = send(&app, "GET", &format!("/snapshot/{s5}/kv/k"), None).await;
        assert_eq!(v["found"], false, "view before GC: deleted");

        // close the long reader: with s5@5 as the only reader the watermark
        // is 5, so GC drops v0..v3 and keeps only the tombstone@5.
        let (s, _) = send(&app, "POST", &format!("/txn/{long}/abort"), None).await;
        assert_eq!(s, StatusCode::OK);
        let (s, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(g["watermark"], 5);
        assert_eq!(g["versions_removed"], 4, "v0,v1,v2,v3 reclaimed");
        assert_eq!(
            g["tombstones_removed"], 0,
            "tombstone@5 is the latest, kept"
        );
        let (s, versions) = send(&app, "GET", "/debug/versions/k", None).await;
        assert_eq!(s, StatusCode::OK);
        let chain = versions["versions"].as_array().unwrap();
        assert_eq!(chain.len(), 1);
        assert_eq!(chain[0]["ts"], 5);
        assert_eq!(chain[0]["deleted"], true);

        // visible results are identical before/after reclamation for the
        // snapshot that was open, and for current readers.
        let (_, v) = send(&app, "GET", &format!("/snapshot/{s5}/kv/k"), None).await;
        assert_eq!(v["found"], false, "view after GC: still deleted");
        let (_, v) = send(&app, "GET", "/kv/k", None).await;
        assert_eq!(v["found"], false);
        let (_, v) = send(&app, "GET", "/kv/k?as_of=5", None).await;
        assert_eq!(v["found"], false);

        // close the snapshot; one more GC drops the tombstone-only chain
        // together with the key; reads still report "not found".
        send(&app, "POST", &format!("/snapshot/{s5}/close"), None).await;
        let (s, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(g["keys_removed"], 1);
        let (_, v) = send(&app, "GET", "/kv/k", None).await;
        assert_eq!(v["found"], false);
        let (_, stats) = send(&app, "GET", "/stats", None).await;
        assert_eq!(stats["keys"], 0);
        assert_eq!(stats["total_versions"], 0);
    }

    /// Acceptance scenario: two concurrent transactions write the same key;
    /// exactly one commits, the other gets 409, and a retry after abort
    /// succeeds with the other writer's value visible.
    #[tokio::test]
    async fn acceptance_concurrent_writers_only_one_commits() {
        let app = app();
        send(&app, "PUT", "/kv/k", Some("\"init\"")).await;

        let t1 = begin(&app).await; // start_ts = 1
        let t2 = begin(&app).await; // start_ts = 1

        let (s, _) = send(&app, "PUT", &format!("/txn/{t1}/kv/k"), Some("\"a\"")).await;
        assert_eq!(s, StatusCode::OK);
        let (s, _) = send(&app, "PUT", &format!("/txn/{t2}/kv/k"), Some("\"b\"")).await;
        assert_eq!(s, StatusCode::OK);

        // first committer wins
        let (s, c1) = send(&app, "POST", &format!("/txn/{t1}/commit"), None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(c1["committed"], true);
        assert_eq!(c1["commit_ts"], 2);

        let (s, c2) = send(&app, "POST", &format!("/txn/{t2}/commit"), None).await;
        assert_eq!(s, StatusCode::CONFLICT);
        assert_eq!(c2["code"], "write_conflict");

        // exactly one version was added on top of init
        let (_, versions) = send(&app, "GET", "/debug/versions/k", None).await;
        let chain = versions["versions"].as_array().unwrap();
        assert_eq!(chain.len(), 2);
        assert_eq!(chain[1]["value"], "a");

        // the loser cannot keep reading/committing: its handle is gone
        let (s, _) = send(&app, "GET", &format!("/txn/{t2}/kv/k"), None).await;
        assert_eq!(s, StatusCode::NOT_FOUND);

        // a fresh transaction (started after ts2) sees the winner's value
        let t3 = begin(&app).await;
        let (_, v) = send(&app, "GET", &format!("/txn/{t3}/kv/k"), None).await;
        assert_eq!(v["value"], "a");
        // and can now commit its own overwrite
        let (s, _) = send(&app, "PUT", &format!("/txn/{t3}/kv/k"), Some("\"c\"")).await;
        assert_eq!(s, StatusCode::OK);
        let (s, c3) = send(&app, "POST", &format!("/txn/{t3}/commit"), None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(c3["commit_ts"], 3);
        let (_, v) = send(&app, "GET", "/kv/k", None).await;
        assert_eq!(v["value"], "c");
    }

    /// Deletes are tombstones: a snapshot taken before the delete keeps
    /// reading the value; history is append-only.
    #[tokio::test]
    async fn delete_is_tombstone_visible_to_older_readers_only() {
        let app = app();
        send(&app, "PUT", "/kv/x", Some("\"alive\"")).await; // ts1
        let sid = snapshot_open_at(&app, 1).await;
        send(&app, "DELETE", "/kv/x", None).await; // ts2

        let (_, v) = send(&app, "GET", &format!("/snapshot/{sid}/kv/x"), None).await;
        assert_eq!(v["value"], "alive");
        let (_, v) = send(&app, "GET", "/kv/x", None).await;
        assert_eq!(v["found"], false);

        let (_, versions) = send(&app, "GET", "/debug/versions/x", None).await;
        let chain = versions["versions"].as_array().unwrap();
        assert_eq!(chain.len(), 2);
        assert_eq!(chain[1]["deleted"], true);
    }

    /// Read-your-writes and abort discarding buffered writes.
    #[tokio::test]
    async fn read_your_writes_and_abort() {
        let app = app();
        send(&app, "PUT", "/kv/k", Some("\"1\"")).await;

        let t = begin(&app).await;
        send(&app, "PUT", &format!("/txn/{t}/kv/k"), Some("\"2\"")).await;
        let (_, v) = send(&app, "GET", &format!("/txn/{t}/kv/k"), None).await;
        assert_eq!(v["value"], "2");

        send(&app, "POST", &format!("/txn/{t}/abort"), None).await;
        let (_, v) = send(&app, "GET", "/kv/k", None).await;
        assert_eq!(v["value"], "1");
    }

    /// GC cannot reclaim versions an open snapshot depends on; layered
    /// snapshots pin progressively more history.
    #[tokio::test]
    async fn gc_pinning_by_open_snapshots() {
        let app = app();
        for val in ["a", "b", "c", "d"] {
            send(&app, "PUT", "/kv/k", Some(&format!("\"{val}\""))).await;
        } // ts1..4

        let s2 = snapshot_open_at(&app, 2).await; // pins k@2
        let s3 = snapshot_open_at(&app, 3).await; // pins k@3

        // GC with watermark 2 keeps k@2,3,4
        let (_, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(g["watermark"], 2);
        assert_eq!(g["versions_removed"], 1); // k@1
        let (_, v) = send(&app, "GET", &format!("/snapshot/{s2}/kv/k"), None).await;
        assert_eq!(v["value"], "b");
        let (_, v) = send(&app, "GET", &format!("/snapshot/{s3}/kv/k"), None).await;
        assert_eq!(v["value"], "c");

        // close s2 -> watermark 3, k@2 becomes reclaimable
        send(&app, "POST", &format!("/snapshot/{s2}/close"), None).await;
        let (_, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(g["watermark"], 3);
        assert_eq!(g["versions_removed"], 1);
        let (_, v) = send(&app, "GET", &format!("/snapshot/{s3}/kv/k"), None).await;
        assert_eq!(v["value"], "c");

        // all snapshots closed -> watermark 4; k@3 dropped, k@4 stays
        send(&app, "POST", &format!("/snapshot/{s3}/close"), None).await;
        let (_, g) = send(&app, "POST", "/gc", None).await;
        assert_eq!(g["watermark"], 4);
        assert_eq!(g["versions_removed"], 1);
        let (_, versions) = send(&app, "GET", "/debug/versions/k", None).await;
        let chain = versions["versions"].as_array().unwrap();
        assert_eq!(chain.len(), 1);
        assert_eq!(chain[0]["value"], "d");
    }

    #[tokio::test]
    async fn snapshot_future_timestamp_rejected() {
        let app = app();
        let (s, v) = send(&app, "POST", "/snapshot/open", Some(r#"{"ts": 99}"#)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST);
        assert_eq!(v["code"], "invalid_timestamp");
    }

    #[tokio::test]
    async fn read_only_commit_advances_nothing() {
        let app = app();
        let t = begin(&app).await;
        let (s, v) = send(&app, "POST", &format!("/txn/{t}/commit"), None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["commit_ts"], 0);
        let (_, stats) = send(&app, "GET", "/stats", None).await;
        assert_eq!(stats["last_commit_ts"], 0);
    }
}
