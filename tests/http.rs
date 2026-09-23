//! End-to-end HTTP test: spawns the real server binary and drives the
//! acceptance scenario (two-level branches, interleaved overwrites, branch
//! deletion, stats) through the REST API.

use reqwest::blocking::Client;
use reqwest::StatusCode;
use serde_json::{json, Value};
use std::net::TcpListener;
use std::process::{Child, Command};
use std::time::{Duration, Instant};
use tempfile::TempDir;

struct Server {
    child: Child,
    base: String,
    _tmp: TempDir,
}

impl Server {
    fn start() -> Server {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let tmp = TempDir::new().unwrap();
        let child = Command::new(env!("CARGO_BIN_EXE_cow-snapstore"))
            .env("COW_DATA_DIR", tmp.path())
            .env("COW_PORT", port.to_string())
            .spawn()
            .unwrap();
        let base = format!("http://127.0.0.1:{port}");
        let client = Client::new();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if let Ok(resp) = client.get(format!("{base}/snapshots")).send() {
                if resp.status().is_success() {
                    break;
                }
            }
            assert!(Instant::now() < deadline, "server did not start");
            std::thread::sleep(Duration::from_millis(50));
        }
        Server { child, base, _tmp: tmp }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn put_page(client: &Client, base: &str, snap: &str, idx: u64, body: &str) {
    let resp = client
        .put(format!("{base}/snapshots/{snap}/pages/{idx}"))
        .body(body.as_bytes().to_vec())
        .send()
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT, "put {snap}/{idx}");
}

fn get_page(client: &Client, base: &str, snap: &str, idx: u64) -> String {
    let resp = client
        .get(format!("{base}/snapshots/{snap}/pages/{idx}"))
        .send()
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK, "get {snap}/{idx}");
    resp.text().unwrap()
}

#[test]
fn http_acceptance_scenario() {
    let server = Server::start();
    let base = &server.base;
    let client = Client::new();

    // Create main and write 6 pages.
    let resp = client
        .post(format!("{base}/snapshots"))
        .json(&json!({"name": "main"}))
        .send()
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CREATED);
    for i in 0..6 {
        put_page(&client, base, "main", i, &format!("main-v1-{i}"));
    }

    // Two levels of branches.
    for (name, from) in [("b1", "main"), ("b2", "b1")] {
        let resp = client
            .post(format!("{base}/snapshots"))
            .json(&json!({"name": name, "from": from}))
            .send()
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
    }
    let stats: Value = client.get(format!("{base}/stats")).send().unwrap().json().unwrap();
    assert_eq!(stats["pages_allocated"], 6, "branching must not copy pages");

    // Interleaved overwrites.
    put_page(&client, base, "main", 0, "main-v2-0");
    put_page(&client, base, "b1", 1, "b1-1");
    put_page(&client, base, "b2", 2, "b2-2");
    put_page(&client, base, "main", 3, "main-v2-3");
    put_page(&client, base, "b2", 0, "b2-0");

    // Delete the middle branch.
    let resp = client.delete(format!("{base}/snapshots/b1")).send().unwrap();
    assert_eq!(resp.status(), StatusCode::NO_CONTENT);

    // Verify surviving snapshot contents.
    assert_eq!(get_page(&client, base, "main", 0), "main-v2-0");
    assert_eq!(get_page(&client, base, "main", 1), "main-v1-1");
    assert_eq!(get_page(&client, base, "main", 3), "main-v2-3");
    assert_eq!(get_page(&client, base, "main", 5), "main-v1-5");
    assert_eq!(get_page(&client, base, "b2", 0), "b2-0");
    assert_eq!(get_page(&client, base, "b2", 2), "b2-2");
    assert_eq!(get_page(&client, base, "b2", 1), "main-v1-1");
    assert_eq!(get_page(&client, base, "b2", 4), "main-v1-4");

    // Deleted snapshot is gone.
    let resp = client.get(format!("{base}/snapshots/b1")).send().unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    let snaps: Vec<String> =
        client.get(format!("{base}/snapshots")).send().unwrap().json().unwrap();
    assert_eq!(snaps, ["b2", "main"]);

    // Stats: 11 allocations total (6 + 5 COW writes). Live = 9: main holds
    // m0,p1,p2,m3,p4,p5; b2 holds b2_0,p1,b2_2,p3,p4,p5. Orphans: p0 (both
    // surviving snapshots overwrote index 0) and b1's copy = 2.
    let stats: Value = client.get(format!("{base}/stats")).send().unwrap().json().unwrap();
    assert_eq!(stats["pages_allocated"], 11);
    assert_eq!(stats["live_pages"], 9);
    assert_eq!(stats["orphan_pages"], 2);

    let gc: Value = client.post(format!("{base}/gc")).send().unwrap().json().unwrap();
    assert_eq!(gc["removed_pages"], 2);
    let stats: Value = client.get(format!("{base}/stats")).send().unwrap().json().unwrap();
    assert_eq!(stats["page_files"], 9);
    assert_eq!(stats["orphan_pages"], 0);

    // Per-snapshot sharing info: main and b2 share p1,p4,p5.
    let info: Value =
        client.get(format!("{base}/snapshots/main")).send().unwrap().json().unwrap();
    assert_eq!(info["private_pages"], 3);
    assert_eq!(info["shared_pages"], 3);

    // Error cases.
    let resp = client
        .post(format!("{base}/snapshots"))
        .json(&json!({"name": "main"}))
        .send()
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    let resp = client.get(format!("{base}/snapshots/main/pages/99")).send().unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}
