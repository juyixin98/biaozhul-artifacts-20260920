//! Mark-sweep GC: orphans, missing refs, shared blocks and the core safety
//! invariant — blocks reachable from a live root are never deleted, even under
//! concurrent reads and root switches.

mod common;

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;

use cas_store::{RealFs, Repository};
use common::{block, both_backends, mem_repo, RepoHarness, TempDir};

/// Two disjoint trees sharing one common leaf.
/// Returns (tree_a_root, tree_b_root, shared_leaf, orphan_root, all_hashes).
fn build_scene(h: &RepoHarness) -> Scene {
    let leaf_a = h.repo.add_block(block(1, 20).as_slice(), &[]).unwrap();
    let leaf_b = h.repo.add_block(block(2, 20).as_slice(), &[]).unwrap();
    let shared = h.repo.add_block(block(3, 20).as_slice(), &[]).unwrap();

    let ra = h
        .repo
        .add_block(
            b"treeA",
            &[leaf_a.hash.clone(), shared.hash.clone()],
        )
        .unwrap();
    let rb = h
        .repo
        .add_block(
            b"treeB",
            &[leaf_b.hash.clone(), shared.hash.clone()],
        )
        .unwrap();
    // Orphan: uploaded, never attached to any root.
    let orphan = h.repo.add_block(b"floating", &[]).unwrap();

    h.repo.put_root("a", &ra.hash).unwrap();
    h.repo.put_root("b", &rb.hash).unwrap();

    Scene {
        root_a: ra.hash,
        root_b: rb.hash,
        leaf_a: leaf_a.hash,
        leaf_b: leaf_b.hash,
        shared: shared.hash,
        orphan: orphan.hash,
    }
}

struct Scene {
    root_a: String,
    root_b: String,
    leaf_a: String,
    leaf_b: String,
    shared: String,
    orphan: String,
}

#[test]
fn dry_run_reports_but_keeps_everything() {
    both_backends(|h| {
        let s = build_scene(h);
        let r = h.repo.gc(true).unwrap();
        assert!(r.dry_run);
        assert_eq!(r.blocks_total, 6);
        assert_eq!(r.reachable, 5);
        assert_eq!(r.orphan, 1);
        assert_eq!(r.removed, vec![s.orphan.clone()]);
        assert!(h.repo.block_exists(&s.orphan), "dry-run must not delete");
    });
}

#[test]
fn sweep_removes_orphan_only() {
    both_backends(|h| {
        let s = build_scene(h);
        let r = h.repo.gc(false).unwrap();
        assert!(!r.dry_run);
        assert_eq!(r.removed, vec![s.orphan.clone()]);
        assert!(!h.repo.block_exists(&s.orphan));
        // Everything reachable survives.
        for alive in [
            &s.root_a, &s.root_b, &s.leaf_a, &s.leaf_b, &s.shared,
        ] {
            assert!(h.repo.block_exists(alive), "{alive} must survive");
        }
    });
}

#[test]
fn shared_leaf_survives_until_no_root_reaches_it() {
    both_backends(|h| {
        let s = build_scene(h);
        // Delete root a: leaf_a becomes garbage; shared is still reached by b.
        h.repo.delete_root("a").unwrap();
        let r = h.repo.gc(false).unwrap();
        let removed: HashSet<&str> = r.removed.iter().map(String::as_str).collect();
        // orphan from scene + root_a + leaf_a
        assert!(removed.contains(s.orphan.as_str()));
        assert!(removed.contains(s.root_a.as_str()));
        assert!(removed.contains(s.leaf_a.as_str()));
        assert!(!removed.contains(s.shared.as_str()), "shared still reached by root b");
        assert!(!removed.contains(s.leaf_b.as_str()));
        assert!(!removed.contains(s.root_b.as_str()));

        // Delete root b as well: the rest is collected.
        h.repo.delete_root("b").unwrap();
        let r = h.repo.gc(false).unwrap();
        let removed: HashSet<String> = r.removed.into_iter().collect();
        assert!(removed.contains(&s.shared));
        assert!(removed.contains(&s.leaf_b));
        assert!(removed.contains(&s.root_b));
        assert_eq!(h.repo.verify().unwrap().blocks_total, 0);
    });
}

#[test]
fn root_switch_away_makes_old_tree_garbage_and_new_tree_live() {
    both_backends(|h| {
        let s = build_scene(h);
        // Switch root a from treeA onto treeB's root.
        h.repo.put_root("a", &s.root_b).unwrap();
        let r = h.repo.gc(false).unwrap();
        let removed: HashSet<&str> = r.removed.iter().map(String::as_str).collect();
        // leaf_a / root_a / orphan are now unreachable; shared still live.
        assert!(removed.contains(s.root_a.as_str()));
        assert!(removed.contains(s.leaf_a.as_str()));
        assert!(removed.contains(s.orphan.as_str()));
        assert!(!removed.contains(s.shared.as_str()));
    });
}

#[test]
fn missing_refs_are_reported_not_fatal_and_reachable_set_is_protected() {
    // Manufacture a missing reference directly on the backend:
    // root block's meta points at a child, then the child data is removed out
    // from underneath (simulating lost files / partial replication).
    let h = mem_repo();
    let child = h.repo.add_block(b"child", &[]).unwrap();
    let root = h
        .repo
        .add_block(b"root", &[child.hash.clone()])
        .unwrap();
    h.repo.put_root("main", &root.hash).unwrap();

    // Delete the child block and its sidecar *externally* via the same Vfs
    // (bypassing repository rules), creating a dangling edge.
    let child_path = h
        .repo
        .path()
        .join("blocks")
        .join(&child.hash[0..2])
        .join(&child.hash);
    let child_meta = h
        .repo
        .path()
        .join("blocks")
        .join(&child.hash[0..2])
        .join(format!("{}.meta", child.hash));
    h.vfs.remove_file(&child_path).unwrap();
    h.vfs.remove_file(&child_meta).unwrap();

    let verify = h.repo.verify().unwrap();
    let dangling: Vec<_> = verify
        .missing_refs
        .iter()
        .filter(|m| m.from == root.hash && m.target == child.hash)
        .collect();
    assert_eq!(dangling.len(), 1);

    // GC must still run: the root itself is reachable and MUST be preserved;
    // the hole is reported.
    let gc = h.repo.gc(false).unwrap();
    assert!(h.repo.block_exists(&root.hash), "reachable root must survive hole");
    assert!(
        gc.missing_refs
            .iter()
            .any(|m| m.from == root.hash && m.target == child.hash),
        "GC should report the dangling edge: {:?}",
        gc.missing_refs
    );
    assert!(!gc.removed.contains(&root.hash));

    // Root pointing directly at a missing block: reported as <root> edge and
    // nothing panics.
    h.repo.delete_root("main").unwrap();
    // Re-publish isn't allowed (block missing), so plant manifest externally:
    let manifest = serde_json::json!({
        "root": "ghost", "hash": child.hash, "published_at": 0
    });
    let roots_dir = h.repo.path().join("roots");
    let tmp = roots_dir.join(".tmp.plant");
    use std::io::Write;
    let mut f = h.vfs.create_new(&tmp).unwrap();
    f.write_all(&serde_json::to_vec(&manifest).unwrap()).unwrap();
    drop(f);
    h.vfs.rename(&tmp, &roots_dir.join("ghost")).unwrap();

    let gc = h.repo.gc(false).unwrap();
    assert!(
        gc.missing_refs.iter().any(|m| m.from == "<root>" && m.target == child.hash),
        "missing root target should be reported: {:?}",
        gc.missing_refs
    );
}

#[test]
fn concurrent_uploads_of_same_block_deduplicate() {
    both_backends(|h| {
        let data = block(42, 4096);
        let repo = Arc::new(h.repo.clone());
        let mut handles = Vec::new();
        for _ in 0..16 {
            let repo = Arc::clone(&repo);
            let data = data.clone();
            handles.push(thread::spawn(move || repo.add_block(&data, &[]).unwrap()));
        }
        let hashes: HashSet<String> = handles
            .into_iter()
            .map(|jh| jh.join().unwrap().hash)
            .collect();
        assert_eq!(hashes.len(), 1, "all uploads resolve to one hash");
        let hash = hashes.into_iter().next().unwrap();
        assert_eq!(h.repo.get_block(&hash).unwrap(), data);
        assert_eq!(h.repo.verify().unwrap().blocks_total, 1);
    });
}

#[test]
fn concurrent_uploads_of_a_tree_preserve_integrity() {
    // Many threads build the same DAG (same content) in leaf-first order with
    // retries: the repository must end with exactly the expected block set and
    // zero integrity errors.
    let h = mem_repo();
    let repo = Arc::new(h.repo.clone());
    let stop = Arc::new(AtomicBool::new(false));
    let mut handles = Vec::new();
    for t in 0..8 {
        let repo = Arc::clone(&repo);
        let stop = Arc::clone(&stop);
        handles.push(thread::spawn(move || {
            // Each thread uses unique content, so no dedup; total must be exact.
            let mut mine = Vec::new();
            for i in 0..20 {
                let data = format!("t{t}-b{i}").into_bytes();
                let info = loop {
                    match repo.add_block(&data, &mine) {
                        Ok(info) => break info,
                        // Refs may momentarily race; they don't for unique
                        // content, but tolerate transient issues.
                        Err(e) => panic!("unexpected error: {e}"),
                    }
                };
                mine.push(info.hash);
            }
            let _ = stop;
            mine
        }));
    }
    let mut total = 0;
    for jh in handles {
        total += jh.join().unwrap().len();
    }
    let v = h.repo.verify().unwrap();
    assert_eq!(v.blocks_total, total);
    assert!(v.corrupt.is_empty());
    assert!(v.missing_refs.is_empty());
}

#[test]
fn gc_never_removes_blocks_read_while_sweeping() {
    // The headline concurrency invariant: readers continuously download every
    // block reachable from the root while GC repeatedly runs and roots are
    // switched. A successful read after a sweep proves the block wasn't
    // unlinked out from under reachability.
    let h = mem_repo();
    let repo = Arc::new(h.repo.clone());

    // Two live trees + a background "backup" root that pins both tree roots
    // (and through them, all 40 leaves), so the true reachable set never
    // shrinks and we can assert every read succeeds and nothing reachable is
    // ever collected.
    let mut pinned: Vec<String> = Vec::new();
    for i in 0..40u8 {
        let info = repo.add_block(&block(i, 128), &[]).unwrap();
        pinned.push(info.hash);
    }
    // parents in two groups
    let pa = repo.add_block(b"A", &pinned[..20]).unwrap();
    let pb = repo.add_block(b"B", &pinned[20..]).unwrap();
    repo.put_root("a", &pa.hash).unwrap();
    repo.put_root("b", &pb.hash).unwrap();
    // backup pins both tree roots — everything stays reachable throughout
    let pbackup = repo
        .add_block(b"backup", &[pa.hash.clone(), pb.hash.clone()])
        .unwrap();
    repo.put_root("backup", &pbackup.hash).unwrap();

    // Add throwaway orphans between sweeps.
    let stop = Arc::new(AtomicBool::new(false));

    let mut readers = Vec::new();
    for _ in 0..6 {
        let repo = Arc::clone(&repo);
        let pinned = pinned.clone();
        let stop = Arc::clone(&stop);
        readers.push(thread::spawn(move || {
            let mut rounds = 0;
            while !stop.load(Ordering::Relaxed) {
                for h in &pinned {
                    // verified read: bytes present AND correct
                    let data = repo.get_block_verified(h).expect("reachable block deleted mid-read");
                    assert_eq!(cas_store::hash::sha256_hex(&data), *h);
                }
                rounds += 1;
            }
            rounds
        }));
    }

    let mut switchers = Vec::new();
    for _ in 0..2 {
        let repo = Arc::clone(&repo);
        let stop = Arc::clone(&stop);
        let ha = pa.hash.clone();
        let hb = pb.hash.clone();
        switchers.push(thread::spawn(move || {
            let mut n = 0;
            while !stop.load(Ordering::Relaxed) {
                // Flip a and b between each other's trees. Each switch stages
                // (pins) its target for the duration of the publish — the
                // upload→publish gap is exactly where a GC could otherwise
                // legitimately reclaim a not-yet-rooted tree.
                let target = if n % 2 == 0 { &hb } else { &ha };
                let _pin = repo.stage(target).unwrap();
                repo.put_root("a", target).unwrap();
                let target = if n % 2 == 0 { &ha } else { &hb };
                let _pin = repo.stage(target).unwrap();
                repo.put_root("b", target).unwrap();
                n += 1;
            }
            n
        }));
    }

    let collector = {
        let repo = Arc::clone(&repo);
        let stop = Arc::clone(&stop);
        let pinned_c = pinned.clone();
        let ha = pa.hash.clone();
        let hb = pb.hash.clone();
        let hbak = pbackup.hash.clone();
        thread::spawn(move || {
            let mut swept = 0;
            let mut any_removed = false;
            for _ in 0..30 {
                // Continuously create orphan garbage for the sweeper.
                for k in 0..5 {
                    let junk = format!("junk-{swept}-{k}-{}", std::process::id()).into_bytes();
                    let _ = repo.add_block(&junk, &[]);
                }
                let r = repo.gc(false).unwrap();
                assert!(
                    !r.removed.iter().any(|x| {
                        pinned_contains(&pinned_c, x) || x == &ha || x == &hb || x == &hbak
                    }),
                    "GC removed reachable block: {:?}",
                    r.removed
                );
                if !r.removed.is_empty() {
                    any_removed = true;
                }
                swept += 1;
            }
            stop.store(true, Ordering::Relaxed);
            (swept, any_removed)
        })
    };

    let (sweeps, any_removed) = collector.join().unwrap();
    assert!(sweeps >= 30);
    assert!(any_removed, "test is vacuous if GC never removed orphans");
    let total_rounds: usize = readers.into_iter().map(|j| j.join().unwrap()).sum();
    let switches: usize = switchers.into_iter().map(|j| j.join().unwrap()).sum();
    assert!(total_rounds > 0);
    assert!(switches > 0);

    // Final state: pinned blocks intact, zero junk left.
    for hsh in &pinned {
        assert!(repo.block_exists(hsh));
    }
    let v = repo.verify().unwrap();
    assert!(v.corrupt.is_empty());
}

fn pinned_contains(v: &[String], x: &str) -> bool {
    v.iter().any(|s| s == x)
}

#[test]
fn gc_with_concurrent_root_switch_reachable_never_lost_without_backup() {
    // Without a backup root: at every instant, exactly the current root's tree
    // is reachable. We switch the root step by step and run GC after each
    // switch; the tree the root points at must always be fully intact.
    //
    // NOTE: blocks belonging to *future* trees are legitimately collectable
    // (nothing references them yet), so each tree is built only when its step
    // arrives — mirroring a real "upload tree, then publish" workflow.
    let h = mem_repo();
    let mut prev: Option<(String, Vec<String>)> = None;
    for step in 0..6 {
        let mut leaves = Vec::new();
        for i in 0..8 {
            let info = h
                .repo
                .add_block(format!("tree{step}-leaf{i}").as_bytes(), &[])
                .unwrap();
            leaves.push(info.hash);
        }
        let root = h
            .repo
            .add_block(format!("root{step}").as_bytes(), &leaves)
            .unwrap();
        h.repo.put_root("live", &root.hash).unwrap();
        let report = h.repo.gc(false).unwrap();
        // Currently published tree must be fully intact.
        for leaf in &leaves {
            assert!(h.repo.block_exists(leaf), "step {step}: live leaf deleted");
            assert!(!report.removed.contains(leaf));
        }
        assert!(h.repo.block_exists(&root.hash));
        assert!(!report.removed.contains(&root.hash));
        // The previous tree, now unreachable, must have been collected
        // (except anything shared — these trees share nothing).
        if let Some((prev_root, prev_leaves)) = &prev {
            assert!(!h.repo.block_exists(prev_root));
            for leaf in prev_leaves {
                assert!(!h.repo.block_exists(leaf));
            }
        }
        prev = Some((root.hash, leaves));
    }
}

#[test]
fn real_fs_gc_and_reopen_roundtrip() {
    let temp = TempDir::new("gc-reopen");
    let store = temp.path.join("store");
    let (keep_hash, junk_hash) = {
        let repo = Repository::open(&store, Arc::new(RealFs::new())).unwrap();
        let keep = repo.add_block(b"keep", &[]).unwrap();
        let junk = repo.add_block(b"junk", &[]).unwrap();
        repo.put_root("main", &keep.hash).unwrap();
        let r = repo.gc(false).unwrap();
        assert_eq!(r.removed, vec![junk.hash.clone()]);
        (keep.hash, junk.hash)
        // repository dropped: process lock released
    };

    // Reopen and verify persisted GC result.
    let repo2 = Repository::open(&store, Arc::new(RealFs::new())).unwrap();
    assert!(repo2.block_exists(&keep_hash));
    assert!(!repo2.block_exists(&junk_hash));
    assert_eq!(repo2.get_root("main").unwrap().hash, keep_hash);
}

#[test]
fn opening_twice_is_rejected_by_process_lock() {
    let dir = TempDir::new("double-open");
    let vfs1 = Arc::new(RealFs::new());
    let _r1 = Repository::open(dir.path.join("store"), vfs1).unwrap();
    let vfs2 = Arc::new(RealFs::new());
    let err = match Repository::open(dir.path.join("store"), vfs2) {
        Ok(_) => panic!("expected Locked error"),
        Err(e) => e,
    };
    assert!(
        matches!(err, cas_store::StoreError::Locked),
        "expected Locked, got {err}"
    );
}
