//! Acceptance scenario, driven deterministically through the HTTP router
//! (no sockets): two writers and one long reader are interleaved by the
//! test itself, which acts as the controlled scheduler.
//!
//! Verified:
//!  1. read consistency — the long reader always observes its pinned
//!     snapshot while writers commit around it;
//!  2. conflict outcome — the second writer touching the same key is
//!     rejected with HTTP 409, then succeeds after re-basing;
//!  3. reclamation — GC cannot reclaim while the snapshot is pinned, and
//!     reclaims space once the snapshot is released.

use std::sync::Arc;

use mvcc_gc::engine::Engine;
use mvcc_gc::json::Json;
use mvcc_gc::server::Server;
use mvcc_gc::vfs::MemVfs;

struct Client {
    server: Server,
}

impl Client {
    fn new() -> Self {
        let engine = Engine::open("/data", Arc::new(MemVfs::new())).unwrap();
        Client {
            server: Server::new(engine, "test"),
        }
    }

    fn call(&self, method: &str, path: &str, body: Option<Json>) -> (u16, Json) {
        let bytes = body.map(|j| j.to_bytes()).unwrap_or_default();
        self.server.handle(method, path, &bytes)
    }

    fn begin(&self) -> (u64, u64) {
        let (s, j) = self.call("POST", "/tx/begin", None);
        assert_eq!(s, 200);
        (
            j.get("tx").unwrap().as_u64().unwrap(),
            j.get("read_version").unwrap().as_u64().unwrap(),
        )
    }

    fn put(&self, tx: u64, key: &str, value: &str) {
        let mut body = Json::obj();
        body.insert("key", Json::string(key));
        body.insert("value", Json::string(value));
        let (s, _) = self.call("POST", &format!("/tx/{tx}/put"), Some(body));
        assert_eq!(s, 200);
    }

    fn commit(&self, tx: u64) -> (u16, Json) {
        self.call("POST", &format!("/tx/{tx}/commit"), None)
    }

    fn get_tx(&self, tx: u64, key: &str) -> Option<String> {
        let mut body = Json::obj();
        body.insert("key", Json::string(key));
        let (s, j) = self.call("POST", &format!("/tx/{tx}/get"), Some(body));
        assert_eq!(s, 200);
        if j.get("found").unwrap().as_bool().unwrap() {
            Some(j.get("value").unwrap().as_str().unwrap().to_string())
        } else {
            None
        }
    }

    fn snapshot(&self) -> (u64, u64) {
        let (s, j) = self.call("POST", "/snapshot", None);
        assert_eq!(s, 200);
        (
            j.get("snapshot").unwrap().as_u64().unwrap(),
            j.get("version").unwrap().as_u64().unwrap(),
        )
    }

    fn get_snapshot(&self, snap: u64, key: &str) -> Option<String> {
        let mut body = Json::obj();
        body.insert("key", Json::string(key));
        let (s, j) = self.call("POST", &format!("/snapshot/{snap}/get"), Some(body));
        assert_eq!(s, 200);
        if j.get("found").unwrap().as_bool().unwrap() {
            Some(j.get("value").unwrap().as_str().unwrap().to_string())
        } else {
            None
        }
    }

    fn stats(&self) -> Json {
        let (s, j) = self.call("GET", "/stats", None);
        assert_eq!(s, 200);
        j
    }

    fn gc(&self) -> Json {
        let (s, j) = self.call("POST", "/gc", None);
        assert_eq!(s, 200);
        j
    }
}

fn live_bytes(stats: &Json) -> u64 {
    stats
        .get("files")
        .unwrap()
        .get("live_bytes")
        .unwrap()
        .as_u64()
        .unwrap()
}

#[test]
fn acceptance_two_writers_one_long_reader() {
    let c = Client::new();

    // --- setup: k=base at v1 -------------------------------------------
    let (t0, _) = c.begin();
    c.put(t0, "k", "base");
    let (s, j) = c.commit(t0);
    assert_eq!(s, 200);
    assert_eq!(j.get("version").unwrap().as_u64().unwrap(), 1);

    // --- long reader pins v1 -------------------------------------------
    let (snap, snap_v) = c.snapshot();
    assert_eq!(snap_v, 1);

    // --- two writers interleave on the same key -------------------------
    let (wa, _) = c.begin(); // writer A @ v1
    let (wb, _) = c.begin(); // writer B @ v1
    c.put(wa, "k", "A");
    c.put(wb, "k", "B");

    // Controlled interleaving: A commits first and wins.
    let (sa, ja) = c.commit(wa);
    assert_eq!(sa, 200);
    assert_eq!(ja.get("version").unwrap().as_u64().unwrap(), 2);

    // B conflicts: HTTP 409.
    let (sb, jb) = c.commit(wb);
    assert_eq!(sb, 409, "writer B must be rejected: {jb:?}");
    assert!(!jb.get("ok").unwrap().as_bool().unwrap());

    // Client-side conflict handling: abort the loser, re-begin at the new
    // version, replay the write, commit.
    let (s, _) = c.call("POST", &format!("/tx/{wb}/abort"), None);
    assert_eq!(s, 200);
    let (wb2, rv) = c.begin();
    assert_eq!(rv, 2);
    c.put(wb2, "k", "B");
    let (sb2, jb2) = c.commit(wb2);
    assert_eq!(sb2, 200);
    assert_eq!(jb2.get("version").unwrap().as_u64().unwrap(), 3);

    // --- read consistency under the pinned snapshot ---------------------
    assert_eq!(c.get_snapshot(snap, "k").as_deref(), Some("base"));
    let (tnow, _) = c.begin();
    assert_eq!(c.get_tx(tnow, "k").as_deref(), Some("B"));
    // An open transaction also pins its read version; close it so only the
    // explicit snapshot keeps the horizon back.
    let (s, _) = c.call("POST", &format!("/tx/{tnow}/abort"), None);
    assert_eq!(s, 200);

    // --- GC while the snapshot is pinned --------------------------------
    let bytes_pinned_before = live_bytes(&c.stats());
    let g1 = c.gc();
    assert_eq!(g1.get("horizon").unwrap().as_u64().unwrap(), 1);
    // The pinned snapshot still reads the old value after GC.
    assert_eq!(c.get_snapshot(snap, "k").as_deref(), Some("base"));
    // Segments newer than the horizon are retained for current readers.
    let stats_mid = c.stats();
    let segs_mid = stats_mid
        .get("files")
        .unwrap()
        .get("segments")
        .unwrap()
        .as_u64()
        .unwrap();
    assert_eq!(segs_mid, 2, "segments 2 and 3 must survive while pinned");

    // --- release the snapshot: space is reclaimed -----------------------
    let (s, _) = c.call("POST", &format!("/snapshot/{snap}/release"), None);
    assert_eq!(s, 200);

    let g2 = c.gc();
    assert_eq!(g2.get("horizon").unwrap().as_u64().unwrap(), 3);
    let reclaimed = g2.get("bytes_reclaimed").unwrap().as_u64().unwrap();
    assert!(reclaimed > 0, "GC must reclaim bytes after release");

    let stats_end = c.stats();
    let files = stats_end.get("files").unwrap();
    assert_eq!(files.get("segments").unwrap().as_u64().unwrap(), 0);
    assert_eq!(files.get("bases").unwrap().as_u64().unwrap(), 1);
    let bytes_end = live_bytes(&stats_end);
    assert!(
        bytes_end < bytes_pinned_before,
        "expected space reclamation: {bytes_pinned_before} -> {bytes_end}"
    );

    // Latest state is intact after reclamation.
    let (tfinal, _) = c.begin();
    assert_eq!(c.get_tx(tfinal, "k").as_deref(), Some("B"));
}

#[test]
fn http_unknown_transaction_is_404() {
    let c = Client::new();
    let mut body = Json::obj();
    body.insert("key", Json::string("k"));
    let (s, _) = c.call("POST", "/tx/999/get", Some(body));
    assert_eq!(s, 404);
}

#[test]
fn http_bad_request_is_400() {
    let c = Client::new();
    let (tx, _) = c.begin();
    let (s, _) = c.call("POST", &format!("/tx/{tx}/put"), Some(Json::obj()));
    assert_eq!(s, 400);
}
