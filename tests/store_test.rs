//! Core store tests: shared subgraphs, root deletion, upload retention, and the
//! headline acceptance property - a root published concurrently with (and
//! restarted during) garbage collection never loses reachable objects.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use refcount_store::Store;

mod common;
use common::TestDir;

fn open() -> (TestDir, Store) {
    let dir = TestDir::new();
    // Tiny retention window so tests can exercise expiry quickly.
    let store = Store::open(dir.path(), 1).unwrap();
    (dir, store)
}

fn block(store: &Store, n: u8) -> String {
    store.put_block(&[n]).unwrap()
}

fn set(items: &[String]) -> BTreeSet<String> {
    items.iter().cloned().collect()
}

/// Build the canonical shared-subgraph fixture:
///
/// ```text
/// rootA -> mA -> shared -> {s1, s2}
/// rootB -> mB -> shared   (mB also owns privB)
/// orphan manifest -> o1   (nothing references it)
/// ```
fn build_shared_graph(store: &Store) -> Graph {
    let s1 = block(store, 1);
    let s2 = block(store, 2);
    let shared = store.put_manifest(&[s1.clone(), s2.clone()]).unwrap();

    let m_a = store.put_manifest(&[shared.clone()]).unwrap();
    let priv_b = block(store, 3);
    let m_b = store
        .put_manifest(&[shared.clone(), priv_b.clone()])
        .unwrap();

    let o1 = block(store, 9);
    let orphan = store.put_manifest(&[o1.clone()]).unwrap();

    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        store.publish_root("A", &m_a).await.unwrap();
        store.publish_root("B", &m_b).await.unwrap();
    });

    Graph {
        s1,
        s2,
        shared,
        m_a,
        priv_b,
        m_b,
        o1,
        orphan,
    }
}

struct Graph {
    s1: String,
    s2: String,
    shared: String,
    m_a: String,
    priv_b: String,
    m_b: String,
    o1: String,
    orphan: String,
}

#[test]
fn content_addressing_is_deterministic() {
    let (_d, s) = open();
    let h1 = s.put_block(b"hello").unwrap();
    let h2 = s.put_block(b"hello").unwrap();
    assert_eq!(h1, h2);
    assert_eq!(s.list_objects().len(), 1);
    assert_eq!(s.get(&h1).unwrap().unwrap(), b"hello");
}

#[test]
fn gc_keeps_shared_subgraph_and_collects_orphan() {
    let (_d, s) = open();
    let g = build_shared_graph(&s);

    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        // Delete root A. The shared subgraph must survive because B reaches it.
        assert!(s.delete_root("A").await.unwrap());
        let report = s.gc().await.unwrap();

        let kept = set(&s.list_objects());
        // Still reachable from B: mB, shared, s1, s2, priv_b.
        for alive in [&g.m_b, &g.shared, &g.s1, &g.s2, &g.priv_b] {
            assert!(kept.contains(alive), "reachable object {alive} was deleted!");
        }
        // A-only manifest and the orphan graph are gone.
        for gone in [&g.m_a, &g.orphan, &g.o1] {
            assert!(!kept.contains(gone), "garbage {gone} was kept");
        }
        assert_eq!(set(&report.deleted), set(&[g.m_a.clone(), g.orphan.clone(), g.o1.clone()]));
        assert_eq!(report.reachable, 5);
    });
}

#[test]
fn deleting_both_roots_collects_everything() {
    let (_d, s) = open();
    let g = build_shared_graph(&s);
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        s.delete_root("A").await.unwrap();
        s.delete_root("B").await.unwrap();
        let report = s.gc().await.unwrap();
        assert!(s.list_objects().is_empty());
        assert_eq!(report.deleted.len(), 8);
        let _ = g;
    });
}

#[test]
fn open_upload_objects_are_retained_then_collected() {
    let (_d, s) = open();
    let rt = tokio::runtime::Runtime::new().unwrap();

    let up = s.begin_upload();
    let h = s.put_for_upload(&up, b"half uploaded").unwrap();
    let m = s
        .put_manifest_for_upload(&up, &[h.clone()])
        .unwrap();

    rt.block_on(async {
        // No root references them, but the open upload retains them.
        let report = s.gc().await.unwrap();
        assert!(s.exists(&h));
        assert!(s.exists(&m));
        assert_eq!(report.open_uploads, 1);
        assert_eq!(report.retained_by_upload, 2);

        // Complete the upload; still within retention window -> retained.
        s.finalize_upload(&up, false).unwrap();
        let report = s.gc().await.unwrap();
        assert!(s.exists(&h));
        assert!(s.exists(&m));
        assert_eq!(report.open_uploads, 0);

        // After the independent retention period elapses, they are garbage.
        // Retention uses whole-second timestamps, so sleep past the 2s boundary
        // to guarantee a >=2s integer-second delta regardless of sub-second
        // phase (avoids a boundary flake).
        tokio::time::sleep(Duration::from_millis(2600)).await;
        let report = s.gc().await.unwrap();
        assert!(!s.exists(&h), "expired upload block should be collected");
        assert!(!s.exists(&m), "expired upload manifest should be collected");
        assert_eq!(report.deleted.len(), 2);
    });
}

#[test]
fn completed_upload_can_be_published_and_then_lives_on() {
    let (_d, s) = open();
    let up = s.begin_upload();
    let b = s.put_for_upload(&up, b"data").unwrap();
    let m = s.put_manifest_for_upload(&up, &[b.clone()]).unwrap();
    s.finalize_upload(&up, false).unwrap();

    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        s.publish_root("fresh", &m).await.unwrap();
        std::thread::sleep(Duration::from_millis(1500));
        let report = s.gc().await.unwrap();
        assert!(s.exists(&m));
        assert!(s.exists(&b));
        assert_eq!(report.deleted.len(), 0);
    });
}

#[test]
fn publishing_dangling_root_is_rejected() {
    let (_d, s) = open();
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        let missing = "a".repeat(64);
        let err = s.publish_root("x", &missing).await.unwrap_err();
        assert!(matches!(err, refcount_store::Error::ObjectNotFound(_)));
    });
}

#[test]
fn data_block_looking_like_manifest_is_not_traversed() {
    let (_d, s) = open();
    // A raw block whose bytes are valid manifest JSON. It must be treated as a
    // leaf because it was put through the block API.
    let h = s.put_block(br#"{"refs":["0000000000000000000000000000000000000000000000000000000000000000"]}"#).unwrap();
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        s.publish_root("r", &h).await.unwrap();
        let report = s.gc().await.unwrap();
        // Only the block itself is reachable; the all-zero "ref" is not a real
        // object and must not cause an error.
        assert_eq!(report.reachable, 1);
        assert!(s.exists(&h));
    });
}

/// The headline concurrency acceptance test.
///
/// One thread hammers GC in a tight loop (a fresh collector "restarting" on
/// every pass). Several publisher threads each own a distinct root name and
/// repeatedly:
///
/// 1. open an *upload session* and stage a brand-new closure into it - while
///    the session is open its objects have an independent retention reason, so
///    GC cannot reap them even though no root points at them yet;
/// 2. delete their current root and publish the new root - two separate
///    critical sections on `gc_lock`, so a GC *can* run in the gap, but during
///    that gap the staged objects are still protected by the open upload;
/// 3. complete the session and assert the now-root-reachable closure is intact.
///
/// Root names are unique per publisher, so after publishing nobody else can
/// remove a publisher's root. A missing closure would mean either GC observed
/// the delete-before-publish window despite the lock or ignored upload
/// retention - precisely what the design forbids. Superseded closures become
/// ordinary garbage once their short-lived upload retention expires.
#[test]
fn concurrent_publish_vs_restarting_gc_never_drops_live_objects() {
    let (_d, s) = open();
    let store = Arc::new(s);

    const PUBLISHERS: u8 = 4;
    const ROUNDS: u8 = 25;

    // Records each publisher's final closure (root manifest, shared, b1, b2).
    let finals = Arc::new(Mutex::new(Vec::<[String; 4]>::new()));
    let mut handles = Vec::new();

    // Collector thread: restart GC constantly throughout the whole workload.
    {
        let st = store.clone();
        handles.push(std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async move {
                for _ in 0..3000 {
                    let _ = st.gc().await;
                }
            });
        }));
    }

    // Publisher threads, each with its own root name.
    for p in 0..PUBLISHERS {
        let st = store.clone();
        let finals = finals.clone();
        handles.push(std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async move {
                let root_name = format!("root-{p}");
                let mut last = None;
                for r in 0..ROUNDS {
                    // 1) Stage a brand-new closure under an open upload so the
                    //    in-flight objects have an independent retention reason.
                    let up = st.begin_upload();
                    let b1 = st.put_for_upload(&up, &[p, r, 1]).unwrap();
                    let b2 = st.put_for_upload(&up, &[p, r, 2]).unwrap();
                    let shared = st
                        .put_manifest_for_upload(&up, &[b1.clone(), b2.clone()])
                        .unwrap();
                    let root_m = st
                        .put_manifest_for_upload(&up, &[shared.clone()])
                        .unwrap();

                    // 2) Replace the root. GC may run in this gap; the open
                    //    upload keeps the new closure alive until published.
                    st.delete_root(&root_name).await.unwrap();
                    st.publish_root(&root_name, &root_m).await.unwrap();

                    // 3) Finish the upload; the closure is now root-reachable.
                    st.finalize_upload(&up, false).unwrap();

                    // Acceptance assertion: the closure the published root now
                    // reaches is fully present.
                    for h in [&root_m, &shared, &b1, &b2] {
                        assert!(
                            st.exists(h),
                            "LIVE OBJECT {h} LOST during concurrent GC (publisher {p} round {r})"
                        );
                    }
                    last = Some([root_m, shared, b1, b2]);
                }
                finals.lock().unwrap().push(last.unwrap());
            });
        }));
    }

    for h in handles {
        h.join().unwrap();
    }

    // Let the per-round upload retention (1s) elapse, then a final GC: the last
    // closure of every publisher survives (4 roots x 4 objects = 16), while all
    // superseded intermediate closures are reclaimed.
    std::thread::sleep(Duration::from_millis(2200));
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        let report = store.gc().await.unwrap();
        let roots = store.list_roots().await;
        assert_eq!(roots.len(), PUBLISHERS as usize);
        for closure in finals.lock().unwrap().iter() {
            for h in closure {
                assert!(store.exists(h), "final closure object {h} must survive");
            }
        }
        assert_eq!(report.reachable, PUBLISHERS as usize * 4, "report: {report:?}");
    });
}
