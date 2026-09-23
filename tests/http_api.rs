//! End-to-end tests against the real HTTP/1.1 protocol (ephemeral port).

mod common;
use cas_store::Repository;


use common::{block, mem_repo, request, HttpServer};

fn start() -> (HttpServer, Repository) {
    let h = mem_repo();
    let repo = h.repo.clone();
    let server = HttpServer::start(h.repo);
    (server, repo)
}

fn post_raw(addr: &str, path: &str, data: &[u8], refs: &[String]) -> common::HttpResponse {
    let xrefs = refs.join(",");
    let headers: Vec<(&str, &str)> = if refs.is_empty() {
        vec![("Content-Type", "application/octet-stream")]
    } else {
        vec![
            ("Content-Type", "application/octet-stream"),
            ("X-Refs", &xrefs),
        ]
    };
    request(addr, "POST", path, &headers, data)
}

#[test]
fn health_and_index() {
    let (s, _repo) = start();
    let r = request(&s.addr, "GET", "/healthz", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["status"], "ok");
    let r = request(&s.addr, "GET", "/", &[], &[]);
    assert_eq!(r.status, 200);
    assert!(r.text().contains("POST   /blocks"));
}

#[test]
fn upload_download_and_head_over_http() {
    let (s, _repo) = start();
    let data = block(1, 500);

    // POST /blocks (server computes hash)
    let r = post_raw(&s.addr, "/blocks", &data, &[]);
    assert_eq!(r.status, 200, "{}", r.text());
    let hash = r.json_value()["hash"].as_str().unwrap().to_string();
    assert_eq!(r.json_value()["deduplicated"], false);

    // Dedup
    let r = post_raw(&s.addr, "/blocks", &data, &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["deduplicated"], true);

    // GET raw
    let r = request(&s.addr, "GET", &format!("/blocks/{hash}"), &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.body, data);

    // HEAD reports size
    let r = request(&s.addr, "HEAD", &format!("/blocks/{hash}"), &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.header("Content-Length").unwrap(), "500");
    assert_eq!(r.header("X-Content-Sha256").unwrap(), hash);

    // GET ?verify=1
    let r = request(&s.addr, "GET", &format!("/blocks/{hash}?verify=1"), &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.body, data);

    // 404
    let ghost = "0".repeat(64);
    let r = request(&s.addr, "GET", &format!("/blocks/{ghost}"), &[], &[]);
    assert_eq!(r.status, 404);
    assert_eq!(r.json_value()["error"], "not_found");
}

#[test]
fn put_block_with_wrong_address_is_rejected() {
    let (s, _repo) = start();
    let wrong = "a".repeat(64);
    let r = request(
        &s.addr,
        "PUT",
        &format!("/blocks/{wrong}"),
        &[("Content-Type", "application/octet-stream")],
        b"data",
    );
    assert_eq!(r.status, 400, "{}", r.text());
    assert_eq!(r.json_value()["error"], "hash_mismatch");
}

#[test]
fn put_block_correct_address_json_hex() {
    let (s, _repo) = start();
    let data = b"json body content";
    let hash = cas_store::hash::sha256_hex(data);
    let body = serde_json::json!({"data": hex_encode(data), "refs": []});
    let r = request(
        &s.addr,
        "PUT",
        &format!("/blocks/{hash}"),
        &[("Content-Type", "application/json")],
        &serde_json::to_vec(&body).unwrap(),
    );
    assert_eq!(r.status, 200, "{}", r.text());
    let r = request(&s.addr, "GET", &format!("/blocks/{hash}"), &[], &[]);
    assert_eq!(r.body, data);
}

#[test]
fn refs_missing_returns_409() {
    let (s, _repo) = start();
    let ghost = "d".repeat(64);
    let r = post_raw(&s.addr, "/blocks", b"child-who", &[ghost]);
    assert_eq!(r.status, 409, "{}", r.text());
    assert_eq!(r.json_value()["error"], "missing_reference");
}

#[test]
fn root_lifecycle_and_gc_over_http() {
    let (s, _repo) = start();

    // Two leaf blocks, one parent
    let l1 = post_raw(&s.addr, "/blocks", b"leaf1", &[]);
    let l2 = post_raw(&s.addr, "/blocks", b"leaf2", &[]);
    let h1 = l1.json_value()["hash"].as_str().unwrap().to_string();
    let h2 = l2.json_value()["hash"].as_str().unwrap().to_string();
    let parent = post_raw(&s.addr, "/blocks", b"parent", &[h1.clone(), h2.clone()]);
    assert_eq!(parent.status, 200, "{}", parent.text());
    let hp = parent.json_value()["hash"].as_str().unwrap().to_string();

    // Orphan block
    let orphan = post_raw(&s.addr, "/blocks", b"orphan", &[]);
    let ho = orphan.json_value()["hash"].as_str().unwrap().to_string();

    // Publish root via X-Block-Hash
    let r = request(
        &s.addr,
        "PUT",
        "/roots/main",
        &[("X-Block-Hash", &hp)],
        &[],
    );
    assert_eq!(r.status, 200, "{}", r.text());

    let r = request(&s.addr, "GET", "/roots/main", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["hash"], hp);

    let r = request(&s.addr, "GET", "/roots", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()[0]["name"], "main");

    // Root at missing block -> 409
    let ghost = "e".repeat(64);
    let r = request(
        &s.addr,
        "PUT",
        "/roots/bad",
        &[("X-Block-Hash", &ghost)],
        &[],
    );
    assert_eq!(r.status, 409);

    // GC dry run: orphan listed, kept
    let r = request(&s.addr, "POST", "/gc?dry_run=1", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["orphan"], 1);
    assert_eq!(r.json_value()["removed"][0], ho);
    let check = request(&s.addr, "HEAD", &format!("/blocks/{ho}"), &[], &[]);
    assert_eq!(check.status, 200);

    // Real sweep removes orphan, keeps reachable three
    let r = request(&s.addr, "POST", "/gc?dry_run=0", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["removed"][0], ho);
    for alive in [&h1, &h2, &hp] {
        let r = request(&s.addr, "HEAD", &format!("/blocks/{alive}"), &[], &[]);
        assert_eq!(r.status, 200, "{alive} reachable but missing");
    }
    let r = request(&s.addr, "HEAD", &format!("/blocks/{ho}"), &[], &[]);
    assert_eq!(r.status, 404);

    // Delete root -> everything collectable on next GC
    let r = request(&s.addr, "DELETE", "/roots/main", &[], &[]);
    assert_eq!(r.status, 200);
    let r = request(&s.addr, "POST", "/gc?dry_run=0", &[], &[]);
    assert_eq!(r.json_value()["orphan"], 3);
    assert_eq!(r.json_value()["reachable"], 0);

    let r = request(&s.addr, "POST", "/verify", &[], &[]);
    assert_eq!(r.status, 200);
    assert_eq!(r.json_value()["blocks_total"], 0);
}

#[test]
fn concurrent_clients_uploading_same_block() {
    let (s, _repo) = start();
    let data = block(9, 4096);
    let addr = s.addr.clone();
    let mut handles = Vec::new();
    for _ in 0..12 {
        let addr = addr.clone();
        let data = data.clone();
        handles.push(std::thread::spawn(move || {
            let r = post_raw(&addr, "/blocks", &data, &[]);
            assert_eq!(r.status, 200, "{}", r.text());
            r.json_value()["hash"].as_str().unwrap().to_string()
        }));
    }
    let hashes: std::collections::HashSet<String> =
        handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(hashes.len(), 1);
    let r = request(&s.addr, "POST", "/verify", &[], &[]);
    assert_eq!(r.json_value()["blocks_total"], 1);
    assert_eq!(r.json_value()["ok"], 1);
}

fn hex_encode(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        use std::fmt::Write;
        let _ = write!(s, "{x:02x}");
    }
    s
}
