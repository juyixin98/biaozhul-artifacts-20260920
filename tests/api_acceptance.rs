//! HTTP 端到端验收测试（不监听真实端口，用 tower::oneshot 驱动 Router）：
//!
//! 验收场景：长读事务跨越三次覆盖 + 删除，验证
//! 1) 旧读不变；
//! 2) 并发写只有一个提交（另一个 409 写-写冲突）；
//! 3) 快照开着时 GC 无法回收其依赖版本；快照关闭后回收，
//!    回收前后“当前可见结果”一致。

use axum::{
    body::{to_bytes, Body},
    http::{Method, Request, StatusCode},
};
use base64::Engine;
use serde_json::Value;
use std::sync::Arc;
use tower::ServiceExt;

use mvcc_kv::app_router;

// 以 lib 方式复用路由（见 src/lib.rs）。
// 注：本文件中 crate 名通过包名引入。

fn b64e(s: &str) -> String {
    base64::engine::general_purpose::URL_SAFE.encode(s)
}

fn b64d_str(s: &str) -> String {
    let b = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(s.trim_end_matches('='))
        .unwrap();
    String::from_utf8(b).unwrap()
}

async fn call(
    app: &axum::Router,
    method: Method,
    uri: &str,
    body: Option<&str>,
) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(uri);
    let body = match body {
        Some(j) => {
            req = req.header("content-type", "application/json");
            Body::from(j.to_string())
        }
        None => Body::empty(),
    };
    let resp = app.clone().oneshot(req.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let json: Value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, json)
}

async fn begin(app: &axum::Router) -> (u64, u64) {
    let (s, v) = call(app, Method::POST, "/txn", None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    (v["txn_id"].as_u64().unwrap(), v["start_ts"].as_u64().unwrap())
}

async fn put_str(app: &axum::Router, txn: u64, key: &str, val: &str) {
    let uri = format!("/txn/{txn}/put/{}", b64e(key));
    let body = format!(r#"{{"value":"{}"}}"#, b64e(val));
    let (s, v) = call(app, Method::POST, &uri, Some(&body)).await;
    assert_eq!(s, StatusCode::OK, "{v}");
}

async fn commit_ok(app: &axum::Router, txn: u64) -> u64 {
    let (s, v) = call(app, Method::POST, &format!("/txn/{txn}/commit"), None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v["commit_ts"].as_u64().unwrap()
}

async fn get_str(app: &axum::Router, txn: u64, key: &str) -> Option<String> {
    let (s, v) = call(
        app,
        Method::GET,
        &format!("/txn/{txn}/get/{}", b64e(key)),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v["value"].as_str().map(b64d_str)
}

#[tokio::test]
async fn acceptance_long_reader_overwrites_delete_conflict_and_gc() {
    let app = app_router(Arc::new(mvcc_kv::MvccStore::new()));

    // 0) 初始 k1=v1。
    let t = begin(&app).await.0;
    put_str(&app, t, "k1", "v1").await;
    commit_ok(&app, t).await;

    // 1) 长读事务开始（此时只能看到 v1）。
    let (reader, reader_ts) = begin(&app).await;
    assert_eq!(get_str(&app, reader, "k1").await, Some("v1".into()));

    // 2) 三次覆盖：v2、v3、v4，各自由独立事务提交。
    for val in ["v2", "v3", "v4"] {
        let w = begin(&app).await.0;
        put_str(&app, w, "k1", val).await;
        commit_ok(&app, w).await;
    }
    // 3) 删除（墓碑）。
    let d = begin(&app).await.0;
    let (s, _) = call(
        &app,
        Method::POST,
        &format!("/txn/{d}/delete/{}", b64e("k1")),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    commit_ok(&app, d).await;

    // 3b) 另一个键 k2：写入后再删除（最终只剩墓碑），用于验证 GC 整条移除。
    let p = begin(&app).await.0;
    put_str(&app, p, "k2", "z1").await;
    commit_ok(&app, p).await;
    let d2 = begin(&app).await.0;
    let (s, _) = call(
        &app,
        Method::POST,
        &format!("/txn/{d2}/delete/{}", b64e("k2")),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    commit_ok(&app, d2).await;

    // 4) 旧读不变：长读事务仍看到 v1。
    assert_eq!(get_str(&app, reader, "k1").await, Some("v1".into()));
    // 新读看到“已删除”，随即关闭该事务，避免它的快照钉住水位线。
    let fresh = begin(&app).await.0;
    assert_eq!(get_str(&app, fresh, "k1").await, None);
    let (s, _) = call(&app, Method::POST, &format!("/txn/{fresh}/rollback"), None).await;
    assert_eq!(s, StatusCode::OK);

    // 版本链此刻有 5 个版本（v1,v2,v3,v4,tomb）。
    let (s, v) = call(
        &app,
        Method::GET,
        &format!("/admin/versions/{}", b64e("k1")),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["versions"].as_array().unwrap().len(), 5);

    // 5) 并发写同一键，只有一个能提交。
    let c1 = begin(&app).await.0;
    let c2 = begin(&app).await.0;
    put_str(&app, c1, "k1", "w1").await;
    put_str(&app, c2, "k1", "w2").await;
    commit_ok(&app, c1).await;
    let (s2, v2) = call(&app, Method::POST, &format!("/txn/{c2}/commit"), None).await;
    assert_eq!(s2, StatusCode::CONFLICT, "expected 409, body={v2}");
    assert_eq!(v2["committed"], false);
    assert_eq!(b64d_str(v2["conflict_key"].as_str().unwrap()), "k1");
    // 中止的事务不能重复提交。
    let (s3, _) = call(&app, Method::POST, &format!("/txn/{c2}/commit"), None).await;
    assert_eq!(s3, StatusCode::NOT_FOUND);

    // c1 的写入对长读事务同样不可见（其快照点更早）。
    assert_eq!(get_str(&app, reader, "k1").await, Some("v1".into()));

    // 6) 快照开着时 GC：水位线 == reader 的 start_ts，v1 不得被回收。
    let (s, gv) = call(&app, Method::POST, "/admin/gc", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(gv["watermark"].as_u64(), Some(reader_ts));
    assert_eq!(gv["versions_reclaimed"].as_u64(), Some(0));
    assert_eq!(get_str(&app, reader, "k1").await, Some("v1".into()));

    // 回收前“当前视图”：k1 = w1（c1 提交后）。
    let before = begin(&app).await.0;
    let visible_before = get_str(&app, before, "k1").await;
    let (_, rollback_resp) = call(
        &app,
        Method::POST,
        &format!("/txn/{before}/rollback"),
        None,
    )
    .await;
    assert_eq!(rollback_resp["rolled_back"].as_u64(), Some(before));

    // 7) 关闭长读事务后再 GC：旧版本回收。
    let (s, _) = call(
        &app,
        Method::POST,
        &format!("/txn/{reader}/rollback"),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (s, wm) = call(&app, Method::GET, "/admin/watermark", None).await;
    assert_eq!(s, StatusCode::OK);
    assert!(wm["watermark"].is_null(), "no live snapshot left");
    let (s, gv) = call(&app, Method::POST, "/admin/gc", None).await;
    assert_eq!(s, StatusCode::OK);
    assert!(gv["versions_reclaimed"].as_u64().unwrap() >= 5);
    // k1 最终值是 w1 不会被移除；只剩墓碑的 k2 被整条移除。
    assert_eq!(gv["keys_removed"].as_u64(), Some(1), "tombstone-only key removed");
    // k2 已不存在。
    let probe = begin(&app).await.0;
    assert_eq!(get_str(&app, probe, "k2").await, None);

    // 8) 回收后“当前可见结果”与回收前一致（k1=w1）。
    let after = probe;
    let visible_after = get_str(&app, after, "k1").await;
    assert_eq!(visible_before, visible_after);
    assert_eq!(visible_after, Some("w1".into()));
    // 版本链裁剪后 k1 只剩 w1 一个版本。
    let (_, vv) = call(
        &app,
        Method::GET,
        &format!("/admin/versions/{}", b64e("k1")),
        None,
    )
    .await;
    assert_eq!(vv["versions"].as_array().unwrap().len(), 1);

    let (s, _) = call(&app, Method::POST, &format!("/txn/{after}/rollback"), None).await;
    assert_eq!(s, StatusCode::OK);
}

#[tokio::test]
async fn readonly_snapshot_reads_consistently_across_gc_until_closed() {
    let app = app_router(Arc::new(mvcc_kv::MvccStore::new()));

    let t = begin(&app).await.0;
    put_str(&app, t, "a", "v1").await;
    commit_ok(&app, t).await;

    // 建立只读快照，停留在 v1。
    let (s, v) = call(&app, Method::POST, "/snapshot", None).await;
    assert_eq!(s, StatusCode::OK);
    let snap = v["snapshot_id"].as_u64().unwrap();

    for val in ["v2", "v3"] {
        let w = begin(&app).await.0;
        put_str(&app, w, "a", val).await;
        commit_ok(&app, w).await;
    }

    // 快照读始终为 v1，即使 GC 也不能动它依赖的版本。
    let snap_get = || async {
        let (s, v) = call(
            &app,
            Method::GET,
            &format!("/snapshot/{snap}/get/{}", b64e("a")),
            None,
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        v["value"].as_str().map(b64d_str)
    };
    assert_eq!(snap_get().await, Some("v1".into()));
    call(&app, Method::POST, "/admin/gc", None).await;
    assert_eq!(snap_get().await, Some("v1".into()));

    // 关闭快照后回收，v1 被清除；当前视图（v3）回收前后不变。
    let cur_before = {
        let x = begin(&app).await.0;
        let r = get_str(&app, x, "a").await;
        call(&app, Method::POST, &format!("/txn/{x}/rollback"), None).await;
        r
    };
    let (s, _) = call(&app, Method::POST, &format!("/snapshot/{snap}"), None).await;
    assert_eq!(s, StatusCode::OK);
    call(&app, Method::POST, "/admin/gc", None).await;
    let cur_after = {
        let x = begin(&app).await.0;
        let r = get_str(&app, x, "a").await;
        call(&app, Method::POST, &format!("/txn/{x}/rollback"), None).await;
        r
    };
    assert_eq!(cur_before, cur_after);
    assert_eq!(cur_after, Some("v3".into()));
}

#[tokio::test]
async fn independent_keys_commit_concurrently_and_scan_is_snapshot_stable() {
    let app = app_router(Arc::new(mvcc_kv::MvccStore::new()));

    let t1 = begin(&app).await.0;
    let t2 = begin(&app).await.0;
    put_str(&app, t1, "a", "1").await;
    put_str(&app, t2, "b", "2").await;
    commit_ok(&app, t1).await;
    commit_ok(&app, t2).await;

    // 在 ts=2 的快照点 scan：a、b 都在。
    let (s, v) = call(&app, Method::GET, "/admin/scan/2", None).await;
    assert_eq!(s, StatusCode::OK);
    let items = v["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(b64d_str(items[0]["key"].as_str().unwrap()), "a");
}
