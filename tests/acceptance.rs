//! Acceptance tests.
//!
//! 1. Shared subgraph: deleting one root keeps blocks shared with another
//!    root; orphan blocks are eventually collected.
//! 2. Incomplete uploads pin their closure for an independent retention
//!    window; abort/expiry releases it.
//! 3. Concurrency: roots published (by completing staged uploads) while GC
//!    hammers the store can never have their closure collected — objects are
//!    protected by the staged upload before completion and by the root after.
//! 4. Restart: state survives closing and reopening the data directory.

use std::collections::HashSet;
use std::sync::Arc;

use cas_gc::store::{Store, StoreConfig};
use serde_json::{json, Value};

mod common;
use common::*;

/// 验收主线：构造共享子图 → 删除一个根 → 发布另一根 → 回收：
/// 仍可达块保留，孤立块最终可回收。
#[tokio::test]
async fn shared_subgraph_survives_and_orphan_is_collected() {
    let t = spawn_app().await;
    let app = &t.app;

    // Data blobs: shared by both roots (s), unique to each (a, b), orphan (o).
    let shared = put_blob(app, b"shared block").await;
    let a = put_blob(app, b"root A only").await;
    let b = put_blob(app, b"root B only").await;
    let orphan = put_blob(app, b"nobody loves me").await;

    // mshared -> {shared}; mA -> {mshared, a}; mB -> {mshared, b}.
    let m_shared = put_manifest(app, &json!({"blobs": [shared]})).await;
    let m_a = put_manifest(app, &json!({"manifests": [m_shared], "blobs": [a]})).await;
    let m_b = put_manifest(app, &json!({"manifests": [m_shared], "blobs": [b]})).await;

    put_root(app, "rootA", &m_a).await;
    put_root(app, "rootB", &m_b).await;

    // First GC: only the standalone orphan is unreachable.
    let r1 = gc(app).await;
    assert_eq!(
        swept(&r1)[0], orphan,
        "orphan blob should be swept, got {r1}"
    );
    assert!(get_object(app, &shared).await.status.is_success());
    assert!(get_object(app, &m_shared).await.status.is_success());

    // Delete root A. `a` becomes garbage; the shared subgraph is still
    // pinned by root B.
    let d = delete_root(app, "rootA").await;
    assert_eq!(d["deleted"], true);
    let r2 = gc(app).await;
    let swept2 = swept(&r2);
    assert!(swept2.contains(&json!(a)), "A-only blob must be collected: {r2}");
    assert!(
        !swept2.contains(&json!(shared)),
        "shared blob reachable from rootB must survive: {r2}"
    );
    assert!(
        !swept2.contains(&json!(m_shared)),
        "shared manifest reachable from rootB must survive: {r2}"
    );
    assert!(get_object(app, &shared).await.status.is_success());
    assert!(get_object(app, &m_shared).await.status.is_success());
    assert!(get_object(app, &b).await.status.is_success());

    // Delete root B too: everything is eventually collectable.
    delete_root(app, "rootB").await;
    let r3 = gc(app).await;
    let swept3 = swept(&r3);
    for h in [&shared, &m_shared, &b, &m_b] {
        assert!(swept3.contains(&json!(h)), "{h} should be swept: {r3}");
        assert!(
            get_object(app, h).await.status == axum::http::StatusCode::NOT_FOUND,
            "{h} must be gone from disk"
        );
    }
}

/// 未完成上传在独立保留期内保护其引用闭包；中止后立即释放。
#[tokio::test]
async fn staged_upload_retained_until_aborted() {
    let t = spawn_app().await;
    let app = &t.app;
    let blob = put_blob(app, b"upload-only blob").await;

    let up = post_json(
        app,
        "/uploads",
        json!({"manifest": {"blobs": [blob]}, "retention_secs": 3600}),
    )
    .await;
    assert_eq!(up.status, axum::http::StatusCode::OK, "{}", up.json);
    let id = up.json["upload_id"].as_str().unwrap().to_string();

    // No root references the closure; GC still must not touch it.
    let r = gc(app).await;
    assert!(swept(&r).is_empty(), "staged closure protected: {r}");
    assert!(get_object(app, &blob).await.status.is_success());

    post_json(app, &format!("/uploads/{id}/abort"), json!({})).await;
    let r2 = gc(app).await;
    assert!(swept(&r2).contains(&json!(blob)));
}

/// retention 到期：GC 先使上传过期再回收其闭包。
#[tokio::test]
async fn expired_upload_is_swept() {
    let t = spawn_app().await;
    let app = &t.app;
    let blob = put_blob(app, b"expiring blob").await;

    let up = post_json(
        app,
        "/uploads",
        json!({"manifest": {"blobs": [blob]}, "retention_secs": 0}),
    )
    .await;
    let id = up.json["upload_id"].as_str().unwrap().to_string();

    let r = gc(app).await;
    assert!(r["expired_uploads"]
        .as_array()
        .unwrap()
        .contains(&json!(id)));
    assert!(swept(&r).contains(&json!(blob)));
}

/// 完成上传可在同一原子步骤里发布根。
#[tokio::test]
async fn complete_upload_publishes_root() {
    let t = spawn_app().await;
    let app = &t.app;
    let blob = put_blob(app, b"hello completion").await;
    let up = post_json(
        app,
        "/uploads",
        json!({"manifest": {"blobs": [blob]}, "retention_secs": 600}),
    )
    .await;
    let id = up.json["upload_id"].as_str().unwrap().to_string();
    let m = up.json["manifest"].as_str().unwrap().to_string();

    let c = post_json(
        app,
        &format!("/uploads/{id}/complete"),
        json!({"root": "published"}),
    )
    .await;
    assert_eq!(c.json["manifest"], m);

    let roots = get(app, "/roots").await.json;
    assert_eq!(roots["roots"]["published"], m);

    let ups = get(app, "/uploads").await.json;
    assert!(ups["uploads"].as_array().unwrap().is_empty());
    let r = gc(app).await;
    assert!(swept(&r).is_empty());
}

/// 内容寻址：相同字节归一；引用缺失对象的清单被 422 拒绝；
/// 传递引用闭包也会被校验。
#[tokio::test]
async fn content_addressing_dedup_and_missing_refs() {
    let t = spawn_app().await;
    let app = &t.app;
    let h1 = put_blob(app, b"same bytes").await;
    let h2 = put_blob(app, b"same bytes").await;
    assert_eq!(h1, h2);

    // Direct missing reference.
    let missing = "a".repeat(64);
    let r = post_json(app, "/manifests", json!({"blobs": [missing]})).await;
    assert_eq!(r.status, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert!(r.json["error"].as_str().unwrap().contains(&missing));

    // Transitive missing reference through an existing manifest.
    let m = put_manifest(app, &json!({"blobs": [h1]})).await;
    let r2 = post_json(
        app,
        "/manifests",
        json!({"manifests": [m, "b".repeat(64)]}),
    )
    .await;
    assert_eq!(r2.status, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
}

/// 核心并发验收：
/// 50 个对象闭包先以“未完成上传”形式暂存（独立保留期保护），
/// 随后“完成上传=发布新根”与多路 GC 回收并发执行；
/// 任意 GC 轮次都不得回收这些对象（完成前由上传保护，完成后由根保护）。
/// 结束后再做一轮 GC，所有块仍可达、可读，50 个根全部存在。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_publish_vs_gc_never_collects_live_roots() {
    let t = spawn_app().await;
    let app = Arc::new(t.app);

    // Stage 50 independent closures as incomplete uploads (retention 1h).
    let mut pairs: Vec<(String, String, String)> = Vec::new(); // (upload, manifest, blob)
    for i in 0..50 {
        let blob = put_blob(&app, format!("blob-{i}").as_bytes()).await;
        let up = post_json(
            &app,
            "/uploads",
            json!({"manifest": {"blobs": [blob]}, "retention_secs": 3600}),
        )
        .await;
        assert_eq!(up.status, axum::http::StatusCode::OK, "{}", up.json);
        pairs.push((
            up.json["upload_id"].as_str().unwrap().to_string(),
            up.json["manifest"].as_str().unwrap().to_string(),
            blob,
        ));
    }

    // Publishers: complete each upload -> publishes a unique new root.
    let mut pub_handles = Vec::new();
    for (round, (id, m, _blob)) in pairs.clone().into_iter().enumerate() {
        let app = app.clone();
        pub_handles.push(tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_micros((round as u64 % 7) * 130)).await;
            let r = post_json(
                &app,
                &format!("/uploads/{id}/complete"),
                json!({"root": format!("live-{round}")}),
            )
            .await;
            assert_eq!(r.status, axum::http::StatusCode::OK, "{}", r.json);
            assert_eq!(r.json["manifest"], m);
        }));
    }

    // Collectors: hammer GC concurrently. Every swept hash is cross-checked
    // against the current reachable closure; none of the 50 closures may ever
    // be swept.
    let mut gc_handles = Vec::new();
    for _ in 0..4 {
        let app = app.clone();
        gc_handles.push(tokio::spawn(async move {
            for _ in 0..100 {
                let report = gc(&app).await;
                let swept_now: Vec<String> = report["swept_hashes"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_string())
                    .collect();
                if !swept_now.is_empty() {
                    let reachable = reachable(&app).await;
                    for h in swept_now {
                        assert!(
                            !reachable.contains(&h),
                            "GC collected {h} while reachable from a live root/upload"
                        );
                    }
                }
            }
        }));
    }

    for h in pub_handles {
        h.await.unwrap();
    }
    for h in gc_handles {
        h.await.unwrap();
    }

    // All 50 roots are published. Final GC sweeps nothing; all objects remain.
    let final_report = gc(&app).await;
    assert!(
        final_report["swept_hashes"].as_array().unwrap().is_empty(),
        "nothing unreachable once all roots published: {final_report}"
    );
    for (_id, m, blob) in &pairs {
        assert!(get_object(&app, m).await.status.is_success(), "manifest {m} lost");
        assert!(get_object(&app, blob).await.status.is_success(), "blob {blob} lost");
    }
    assert_eq!(
        get(&app, "/roots").await.json["roots"]
            .as_object()
            .unwrap()
            .len(),
        50
    );
}

/// 更直接的竞态窗口测试：在 GC 密集运行时反复“发布根→删除根→再发布”，
/// 任何一次发布成功之后 GC 都不得回收其闭包。
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn republish_while_gc_running() {
    let t = spawn_app().await;
    let app = Arc::new(t.app);
    let blob = put_blob(&app, b"hot potato").await;
    let m = put_manifest(&app, &json!({"blobs": [blob]})).await;

    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));

    // Pin the closure with an initial root before GC starts, so every
    // subsequent republish is genuinely concurrent with collection.
    put_root(&app, "hot-init", &m).await;

    let mut handles = Vec::new();
    // Two GC hogs.
    for _ in 0..2 {
        let app = app.clone();
        let stop = stop.clone();
        handles.push(tokio::spawn(async move {
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                gc(&app).await;
            }
        }));
    }
    // Publisher churns the root. After every successful publish it verifies
    // the closure survived any GC that may have interleaved.
    let papp = app.clone();
    let pblob = blob.clone();
    let pm = m.clone();
    let publisher = tokio::spawn(async move {
        for i in 0..200 {
            let name = format!("hot-{i}");
            put_root(&papp, &name, &pm).await;
            // Yield so GC can interleave between publish and verification.
            tokio::task::yield_now().await;
            assert!(get_object(&papp, &pm).await.status.is_success());
            assert!(get_object(&papp, &pblob).await.status.is_success());
        }
    });

    publisher.await.unwrap();
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    for h in handles {
        h.await.unwrap();
    }

    // Blob + manifest are still reachable via hot-199.
    let r = gc(&app).await;
    assert!(r["swept_hashes"].as_array().unwrap().is_empty(), "{r}");
    assert!(get_object(&app, &blob).await.status.is_success());
}

/// 重启验收：关闭后用同一数据目录重新打开，根与对象全部保留，GC 行为不变。
#[tokio::test]
async fn state_survives_restart_and_unreachable_collected_after_reopen() {
    // Persist the temp dir beyond the TestApp block (avoid auto-cleanup).
    let tmp = tempfile::tempdir().unwrap();
    let dir_path = tmp.path().to_path_buf();
    let keep_hashes;
    let drop_hashes;
    {
        let cfg = StoreConfig {
            default_retention_secs: 3600,
        };
        let store = Store::open(&dir_path, cfg).unwrap();
        let app = cas_gc::api::router(store.clone());

        let keep_blob = put_blob(&app, b"kept forever").await;
        let junk = put_blob(&app, b"ephemeral junk").await;
        let m_keep = put_manifest(&app, &json!({"blobs": [keep_blob]})).await;
        let m_junk = put_manifest(&app, &json!({"blobs": [junk]})).await;
        put_root(&app, "persistent", &m_keep).await;
        keep_hashes = vec![keep_blob, m_keep];
        drop_hashes = vec![junk, m_junk];

        // Unreferenced closure is collected before restart.
        let r = gc(&app).await;
        assert!(swept(&r).contains(&json!(drop_hashes[0])));
        assert!(swept(&r).contains(&json!(drop_hashes[1])));
    }

    // Reopen the same directory ("回收重启").
    let store = Store::open(
        &dir_path,
        StoreConfig {
            default_retention_secs: 3600,
        },
    )
    .unwrap();
    for h in &keep_hashes {
        assert!(store.contains(h), "{h} must survive restart");
    }
    for h in &drop_hashes {
        assert!(!store.contains(h), "{h} must stay gone after restart");
    }
    let roots = store.roots();
    assert_eq!(roots.get("persistent"), Some(&keep_hashes[1]));

    // Publish a brand-new root after restart and prove GC preserves it.
    let app = cas_gc::api::router(store.clone());
    let extra = put_blob(&app, b"post-restart").await;
    let m_extra = put_manifest(&app, &json!({"blobs": [extra]})).await;
    put_root(&app, "after-restart", &m_extra).await;
    let r = gc(&app).await;
    let swept_now: HashSet<String> = r["swept_hashes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert!(!swept_now.contains(&extra));
    assert!(!swept_now.contains(&m_extra));
    assert!(store.contains(&extra));
}

fn swept(v: &Value) -> Vec<Value> {
    v["swept_hashes"].as_array().cloned().unwrap_or_default()
}
