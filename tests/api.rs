//! End-to-end API tests driven through the Axum router in-process.
//!
//! Covered acceptance scenarios:
//! * out-of-order part upload + final read/hash verification
//! * completing with missing parts (422), status endpoint reports the gap
//! * idempotent identical re-upload (200) vs different-content re-upload (409)
//! * repeated complete calls (idempotent, also after restart)
//! * object is unreadable before publish (404)
//! * whole-object sha256 mismatch rejected and stays unreadable after restart
//! * immutability: republishing an existing key / writing to committed upload
//! * crash + restart: stale tmp swept, upload resumable, reconciliation heals a
//!   session whose committed-flag write was lost, and only complete,
//!   hash-correct objects are ever readable
//! * zero-length object, unknown session/part handling

use std::path::PathBuf;

use axum::body::Body;
use axum::Router;
use bytes::Bytes;
use http::{Method, Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::Value;
use tempfile::TempDir;
use tower::ServiceExt;

use chunk_upload::test_support::{router_on, AppCfg};

struct Ctx {
    _dir: TempDir,
    root: PathBuf,
}

impl Ctx {
    fn new() -> Self {
        let dir = TempDir::new().unwrap();
        Ctx {
            root: dir.path().to_path_buf(),
            _dir: dir,
        }
    }
    async fn app(&self) -> Router {
        router_on(AppCfg {
            data_dir: self.root.clone(),
            max_body: 64 * 1024 * 1024,
        })
        .await
    }
}

async fn send(
    app: Router,
    method: Method,
    uri: &str,
    json: Option<Value>,
    raw: Option<Vec<u8>>,
) -> (StatusCode, Bytes) {
    let mut req = Request::builder().method(method).uri(uri);
    let body = if let Some(v) = json {
        req = req.header("content-type", "application/json");
        Body::from(v.to_string())
    } else if let Some(b) = raw {
        req = req.header("content-type", "application/octet-stream");
        Body::from(b)
    } else {
        Body::empty()
    };
    let resp = app.oneshot(req.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (status, bytes)
}

async fn json_req(app: Router, m: Method, uri: &str, v: Value) -> (StatusCode, Value) {
    let (s, b) = send(app, m, uri, Some(v), None).await;
    (s, serde_json::from_slice(&b).unwrap_or(Value::Null))
}
async fn raw_req(app: Router, m: Method, uri: &str, b: Vec<u8>) -> (StatusCode, Value) {
    let (s, bytes) = send(app, m, uri, None, Some(b)).await;
    (s, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}
async fn empty_req(app: Router, m: Method, uri: &str) -> (StatusCode, Value) {
    let (s, b) = send(app, m, uri, None, None).await;
    (s, serde_json::from_slice(&b).unwrap_or(Value::Null))
}
async fn get_obj(app: Router, uri: &str) -> (StatusCode, Bytes) {
    send(app, Method::GET, uri, None, None).await
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

/// Create a session, returning (upload_id, chunks as Vec<(start,end)>).
async fn create(ctx: &Ctx, key: &str, data: &[u8], chunk: u64) -> (String, Vec<(u64, u64)>) {
    let body = serde_json::json!({
        "key": key,
        "total_size": data.len(),
        "sha256": sha256_hex(data),
        "chunk_size": chunk,
    });
    let (s, v) = json_req(ctx.app().await, Method::POST, "/uploads", body).await;
    assert_eq!(s, StatusCode::CREATED, "create failed: {v}");
    let id = v["upload_id"].as_str().unwrap().to_string();
    let chunks = v["chunks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| (c["start"].as_u64().unwrap(), c["end"].as_u64().unwrap()))
        .collect();
    (id, chunks)
}

async fn put_part(ctx: &Ctx, id: &str, n: u32, bytes: Vec<u8>) -> (StatusCode, Value) {
    raw_req(
        ctx.app().await,
        Method::PUT,
        &format!("/uploads/{id}/parts/{n}"),
        bytes,
    )
    .await
}

#[tokio::test]
async fn out_of_order_upload_then_read() {
    let ctx = Ctx::new();
    // 10 bytes, 3-byte chunks -> [0,3) [3,6) [6,9) [9,10)
    let data = b"0123456789".to_vec();
    let (id, chunks) = create(&ctx, "logs/a.txt", &data, 3).await;
    assert_eq!(chunks, vec![(0, 3), (3, 6), (6, 9), (9, 10)]);

    // Not readable before publication.
    let (s, _) = get_obj(ctx.app().await, "/objects/logs/a.txt").await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    // Upload in scrambled order: 2, 0, 3, 1.
    for n in [2u32, 0, 3, 1] {
        let (s, e) = chunks[n as usize];
        let slice = data[s as usize..e as usize].to_vec();
        let (st, v) = put_part(&ctx, &id, n, slice).await;
        assert_eq!(st, StatusCode::OK, "part {n}: {v}");
        assert_eq!(v["number"], n);
    }

    let (s, v) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "complete: {v}");
    assert_eq!(v["size"], 10);
    assert_eq!(v["sha256"], sha256_hex(&data));

    let (s, bytes) = get_obj(ctx.app().await, "/objects/logs/a.txt").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(&bytes[..], &data[..]);
}

#[tokio::test]
async fn missing_parts_blocks_completion() {
    let ctx = Ctx::new();
    let data = b"abcdefghij".to_vec();
    let (id, chunks) = create(&ctx, "k", &data, 4).await; // parts 0,1,2
    // Only upload parts 0 and 2.
    for n in [0u32, 2] {
        let (s, e) = chunks[n as usize];
        let (st, _) = put_part(&ctx, &id, n, data[s as usize..e as usize].to_vec()).await;
        assert_eq!(st, StatusCode::OK);
    }
    let (s, v) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "body: {v}");
    assert_eq!(v["error"], "unprocessable_entity");

    // Status endpoint reports the gap.
    let (s, v) = empty_req(
        ctx.app().await,
        Method::GET,
        &format!("/uploads/{id}"),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["missing_parts"][0], 1);
    assert_eq!(v["received_parts"].as_array().unwrap().len(), 2);

    // Object still not published after a "restart".
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s, _) = get_obj(app, "/objects/k").await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn part_reupload_identical_is_idempotent_different_rejected() {
    let ctx = Ctx::new();
    let data = b"xyzxyz".to_vec();
    let (id, chunks) = create(&ctx, "idem", &data, 3).await;
    let (s0, e0) = chunks[0];
    let p0 = data[s0 as usize..e0 as usize].to_vec();

    let (st, v1) = put_part(&ctx, &id, 0, p0.clone()).await;
    assert_eq!(st, StatusCode::OK, "{v1}");
    let hash1 = v1["sha256"].as_str().unwrap().to_string();

    // Identical retransmit -> 200, same hash.
    let (st, v2) = put_part(&ctx, &id, 0, p0.clone()).await;
    assert_eq!(st, StatusCode::OK, "{v2}");
    assert_eq!(v2["sha256"], hash1);

    // Different content for same part number -> 409.
    let (st, v3) = put_part(&ctx, &id, 0, b"abc".to_vec()).await;
    assert_eq!(st, StatusCode::CONFLICT, "{v3}");
    assert_eq!(v3["error"], "part_conflict");

    // Correct completion still works (stored part untouched).
    let (s1, e1) = chunks[1];
    let (st, _) = put_part(&ctx, &id, 1, data[s1 as usize..e1 as usize].to_vec()).await;
    assert_eq!(st, StatusCode::OK);
    let (st, v) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
}

#[tokio::test]
async fn repeated_complete_is_idempotent() {
    let ctx = Ctx::new();
    let data = b"hello world".to_vec();
    let (id, chunks) = create(&ctx, "dup", &data, 100).await;
    for (n, (s, e)) in chunks.iter().enumerate() {
        let (st, _) = put_part(&ctx, &id, n as u32, data[*s as usize..*e as usize].to_vec()).await;
        assert_eq!(st, StatusCode::OK);
    }
    let url = format!("/uploads/{id}/complete");
    let (s1, v1) = empty_req(ctx.app().await, Method::POST, &url).await;
    assert_eq!(s1, StatusCode::CREATED);
    let (s2, v2) = empty_req(ctx.app().await, Method::POST, &url).await;
    assert_eq!(s2, StatusCode::CREATED, "second complete: {v2}");
    assert_eq!(v1["sha256"], v2["sha256"]);
    assert_eq!(v1["upload_id"], v2["upload_id"]);

    // A third time after restart.
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s3, v3) = empty_req(app, Method::POST, &url).await;
    assert_eq!(s3, StatusCode::CREATED, "post-restart complete: {v3}");
}

#[tokio::test]
async fn wrong_total_hash_is_rejected_and_unreadable_after_restart() {
    let ctx = Ctx::new();
    let data = b"0123456789".to_vec();
    // Declare a bogus hash.
    let body = serde_json::json!({
        "key": "bad",
        "total_size": data.len(),
        "sha256": "00".repeat(32),
        "chunk_size": 4,
    });
    let (s, v) = json_req(ctx.app().await, Method::POST, "/uploads", body).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    let id = v["upload_id"].as_str().unwrap().to_string();
    let chunks: Vec<(u64, u64)> = v["chunks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| (c["start"].as_u64().unwrap(), c["end"].as_u64().unwrap()))
        .collect();
    for (n, (s0, e0)) in chunks.iter().enumerate() {
        let (st, _) = put_part(&ctx, &id, n as u32, data[*s0 as usize..*e0 as usize].to_vec()).await;
        assert_eq!(st, StatusCode::OK);
    }
    let (s, v) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "{v}");
    assert!(v["message"].as_str().unwrap().contains("sha256 mismatch"));

    // Simulate a server restart: reopen the same data directory.
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s, _) = get_obj(app, "/objects/bad").await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn published_object_is_immutable() {
    let ctx = Ctx::new();
    let data = b"immutable".to_vec();
    let (id, chunks) = create(&ctx, "same-key", &data, 100).await;
    for (n, (s, e)) in chunks.iter().enumerate() {
        let (st, _) = put_part(&ctx, &id, n as u32, data[*s as usize..*e as usize].to_vec()).await;
        assert_eq!(st, StatusCode::OK);
    }
    let (s, _) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);

    // Creating a new upload for the same key is rejected.
    let body = serde_json::json!({
        "key": "same-key",
        "total_size": data.len(),
        "sha256": sha256_hex(&data),
    });
    let (s, v) = json_req(ctx.app().await, Method::POST, "/uploads", body).await;
    assert_eq!(s, StatusCode::CONFLICT, "{v}");

    // Uploading more parts to the already-committed session is rejected too.
    let (s0, _e0) = chunks[0];
    let (st, _) = put_part(&ctx, &id, 0, data[s0 as usize..].to_vec()).await;
    assert_eq!(st, StatusCode::CONFLICT);
}

#[tokio::test]
async fn restart_after_crash_heals_state() {
    let ctx = Ctx::new();
    let data: Vec<u8> = (0..300u32).map(|i| (i % 251) as u8).collect();
    let (id, chunks) = create(&ctx, "crash/obj.bin", &data, 64).await;

    // Upload part 0 only, then leave a stale .tmp file as a crashed write would.
    let (s0, e0) = chunks[0];
    let (st, _) = put_part(&ctx, &id, 0, data[s0 as usize..e0 as usize].to_vec()).await;
    assert_eq!(st, StatusCode::OK);
    let stale_tmp = ctx
        .root
        .join("uploads")
        .join(&id)
        .join("parts")
        .join("1.tmp");
    tokio::fs::write(&stale_tmp, b"partial").await.unwrap();

    // "Crash": reopen the same directory with a brand-new store instance.
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    // Stale tmp swept, part 0 present, part 1 still missing.
    assert!(!stale_tmp.exists(), "stale tmp should be swept on startup");
    let (s, v) = empty_req(app, Method::GET, &format!("/uploads/{id}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["received_parts"][0], 0);
    assert!(v["missing_parts"]
        .as_array()
        .unwrap()
        .contains(&Value::from(1)));

    // Finish the remaining parts after restart.
    for (n, (s1, e1)) in chunks.iter().enumerate().skip(1) {
        let (st, _) = put_part(&ctx, &id, n as u32, data[*s1 as usize..*e1 as usize].to_vec()).await;
        assert_eq!(st, StatusCode::OK);
    }
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s, v) = empty_req(app, Method::POST, &format!("/uploads/{id}/complete")).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");

    // Simulate losing the committed flag (crash between object rename and flag write).
    let meta_path = ctx.root.join("uploads").join(&id).join("meta.json");
    let mut meta: Value =
        serde_json::from_slice(&tokio::fs::read(&meta_path).await.unwrap()).unwrap();
    assert_eq!(meta["committed"], true);
    meta["committed"] = Value::Bool(false);
    tokio::fs::write(&meta_path, serde_json::to_vec_pretty(&meta).unwrap())
        .await
        .unwrap();

    // Reopen: reconciliation observes the object and heals the flag; the object
    // reads back byte-identical.
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s, bytes) = get_obj(app, "/objects/crash/obj.bin").await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(&bytes[..], &data[..]);
    let app = router_on(AppCfg {
        data_dir: ctx.root.clone(),
        max_body: 64 * 1024 * 1024,
    })
    .await;
    let (s, v) = empty_req(app, Method::GET, &format!("/uploads/{id}")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["committed"], true);
}

#[tokio::test]
async fn zero_length_object_roundtrips() {
    let ctx = Ctx::new();
    let data: Vec<u8> = vec![];
    let (id, chunks) = create(&ctx, "empty", &data, 4).await;
    assert_eq!(chunks, vec![(0, 0)]); // exactly one empty part
    let (st, v) = put_part(&ctx, &id, 0, vec![]).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    let (st, v) = empty_req(
        ctx.app().await,
        Method::POST,
        &format!("/uploads/{id}/complete"),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{v}");
    let (s, bytes) = get_obj(ctx.app().await, "/objects/empty").await;
    assert_eq!(s, StatusCode::OK);
    assert!(bytes.is_empty());
    assert_eq!(v["sha256"], sha256_hex(b""));
}

#[tokio::test]
async fn unknown_sessions_and_parts_404() {
    let ctx = Ctx::new();
    let (s, _) = empty_req(ctx.app().await, Method::GET, "/uploads/doesnotexist").await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = raw_req(
        ctx.app().await,
        Method::PUT,
        "/uploads/ghost/parts/0",
        b"x".to_vec(),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = get_obj(ctx.app().await, "/objects/nope").await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn wrong_part_length_rejected() {
    let ctx = Ctx::new();
    let data = b"abcd".to_vec(); // one 4-byte part
    let (id, _chunks) = create(&ctx, "len", &data, 4).await;
    let (st, v) = put_part(&ctx, &id, 0, b"abc".to_vec()).await; // 3 != 4
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
    let (st, v) = put_part(&ctx, &id, 7, b"abc".to_vec()).await; // out of range
    assert_eq!(st, StatusCode::BAD_REQUEST, "{v}");
}
