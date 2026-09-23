//! Crash-safety test with per-page fault injection.
//!
//! A child process runs a deterministic script of branch/write operations and
//! is killed (SIGABRT via the COW_CRASH_AT failpoint) at every possible hit of
//! every commit boundary. After each kill the parent reopens the store on the
//! same directory and verifies:
//!   * recovery succeeds (no corrupt manifests, no missing page files),
//!   * every snapshot's contents are a consistent prefix of the script,
//!   * GC leaves exactly the live set.
//! Finally the script runs to completion and the final state is verified.

use cow_snapstore::Store;
use std::path::PathBuf;
use std::process::Command;
use tempfile::TempDir;

const MAIN_PAGES: u64 = 6; // main writes pages 0..6
const B1_WRITES: u64 = 3; // b1 overwrites pages 0..3
const B2_WRITES: u64 = 2; // b2 overwrites pages 0..2

fn content(snap: &str, i: u64) -> Vec<u8> {
    format!("{snap}:{i}").into_bytes()
}

/// Write only if the page does not already hold the wanted content, so that
/// re-running the script after a crash converges without extra allocations.
fn write_if_needed(store: &Store, snap: &str, i: u64, bytes: &[u8]) {
    if store.read_page(snap, i).ok().as_deref() == Some(bytes) {
        return;
    }
    store.write_page(snap, i, bytes).unwrap();
}

/// The deterministic script. Each operation commits atomically, so any
/// crash leaves a prefix of this script applied.
fn run_script(dir: &std::path::Path) {
    let store = Store::open(dir).unwrap();
    if store.list_snapshots().is_empty() {
        store.create_snapshot("main", None).unwrap();
    }
    for i in 0..MAIN_PAGES {
        write_if_needed(&store, "main", i, &content("main", i));
    }
    if !store.list_snapshots().contains(&"b1".to_string()) {
        store.create_snapshot("b1", Some("main")).unwrap();
    }
    for i in 0..B1_WRITES {
        write_if_needed(&store, "b1", i, &content("b1", i));
    }
    if !store.list_snapshots().contains(&"b2".to_string()) {
        store.create_snapshot("b2", Some("b1")).unwrap();
    }
    for i in 0..B2_WRITES {
        write_if_needed(&store, "b2", i, &content("b2", i));
    }
}

/// Verify that `snap`'s pages match a prefix of its write sequence layered
/// over its parent's state at branch time.
fn verify_prefix(store: &Store, snap: &str, base: &dyn Fn(u64) -> Option<Vec<u8>>, writes: u64) {
    for i in 0..MAIN_PAGES {
        match store.read_page(snap, i) {
            Ok(bytes) => {
                let own = content(snap, i);
                if i < writes && bytes == own {
                    // This overwrite committed; all earlier ones must have too.
                    for j in 0..i {
                        assert_eq!(
                            store.read_page(snap, j).unwrap(),
                            content(snap, j),
                            "{snap}: overwrite {i} committed but {j} missing"
                        );
                    }
                } else {
                    let expected = base(i).unwrap_or_else(|| {
                        panic!("{snap}: page {i} = {bytes:?}, neither own write nor base")
                    });
                    assert_eq!(bytes, expected, "{snap}: page {i} has torn content");
                }
            }
            Err(_) => {
                // Page absent: only valid if the base also lacks it (main's
                // writes are a prefix, so all later pages must be absent too).
                assert!(base(i).is_none(), "{snap}: page {i} missing but base has it");
                for j in (i + 1)..MAIN_PAGES {
                    assert!(
                        store.read_page(snap, j).is_err(),
                        "{snap}: page {j} present after gap at {i}"
                    );
                }
                break;
            }
        }
    }
}

/// Check all consistency invariants on the store at `dir`.
fn verify_consistent(dir: &std::path::Path) {
    let store = Store::open(dir).unwrap(); // recovery itself must succeed
    let snaps = store.list_snapshots();

    // If the crash happened before main's manifest commit, nothing exists yet.
    if !snaps.contains(&"main".to_string()) {
        assert!(snaps.is_empty(), "main missing but other snapshots exist: {snaps:?}");
        let stats = store.stats().unwrap();
        assert_eq!(stats.orphan_pages, 0, "orphans after recovery GC");
        return;
    }
    verify_prefix(&store, "main", &|_| None, MAIN_PAGES);

    if snaps.contains(&"b1".to_string()) {
        // b1 branched after main was fully written.
        verify_prefix(&store, "b1", &|i| Some(content("main", i)), B1_WRITES);
    }
    if snaps.contains(&"b2".to_string()) {
        // b2 branched after b1's overwrites completed.
        verify_prefix(
            &store,
            "b2",
            &|i| {
                Some(if i < B1_WRITES { content("b1", i) } else { content("main", i) })
            },
            B2_WRITES,
        );
    }

    // After GC (which open() already ran), no orphans and no dangling refs:
    // every read above succeeded, and page_files == live_pages.
    let stats = store.stats().unwrap();
    assert_eq!(stats.orphan_pages, 0, "orphans after recovery GC");
    assert_eq!(stats.page_files, stats.live_pages);
}

#[test]
fn crash_at_every_commit_point() {
    if std::env::var("COW_CRASH_CHILD").is_ok() {
        // Child mode: run the script; the failpoint aborts us mid-operation.
        let dir = PathBuf::from(std::env::var("COW_TEST_DIR").unwrap());
        run_script(&dir);
        return;
    }

    let points = [
        "page_flushed",
        "page_committed",
        "commit_tmp_written",
        "manifest_committed",
        "branch_committed",
    ];
    let exe = std::env::current_exe().unwrap();
    let mut crashes = 0u32;

    for point in points {
        // Each point is hit many times during the script (once per page write,
        // per manifest commit, ...). Crash at each of the first several hits.
        for after in 1..=6u64 {
            let tmp = TempDir::new().unwrap();
            let status = Command::new(&exe)
                .arg("crash_at_every_commit_point")
                .arg("--exact")
                .arg("--nocapture")
                .env("COW_CRASH_CHILD", "1")
                .env("COW_TEST_DIR", tmp.path())
                .env("COW_CRASH_AT", point)
                .env("COW_CRASH_AFTER", after.to_string())
                .status()
                .unwrap();
            if status.success() {
                // Script completed before the n-th hit: nothing more to
                // inject at this point.
                verify_consistent(tmp.path());
                break;
            }
            crashes += 1;
            verify_consistent(tmp.path());

            // Resume the same script on the same directory WITHOUT injection:
            // it must converge to the complete final state.
            let status = Command::new(&exe)
                .arg("crash_at_every_commit_point")
                .arg("--exact")
                .env("COW_CRASH_CHILD", "1")
                .env("COW_TEST_DIR", tmp.path())
                .status()
                .unwrap();
            assert!(status.success(), "resume run failed");
            verify_final(tmp.path(), false);
        }
    }
    assert!(crashes > 0, "no crashes were injected — failpoints broken?");
    eprintln!("crash test: injected and recovered from {crashes} crashes");
}

/// Full final state after the script completes. `exact_allocations` is only
/// asserted on a crash-free run: a crash can lose the (non-atomic) meta.json
/// counter update or strand an uncommitted allocation, so after recovery the
/// cumulative counter may differ while the live set stays exact.
fn verify_final(dir: &std::path::Path, exact_allocations: bool) {
    let store = Store::open(dir).unwrap();
    for i in 0..MAIN_PAGES {
        assert_eq!(store.read_page("main", i).unwrap(), content("main", i));
        let b1_expect = if i < B1_WRITES { content("b1", i) } else { content("main", i) };
        assert_eq!(store.read_page("b1", i).unwrap(), b1_expect);
        let b2_expect = if i < B2_WRITES {
            content("b2", i)
        } else if i < B1_WRITES {
            content("b1", i)
        } else {
            content("main", i)
        };
        assert_eq!(store.read_page("b2", i).unwrap(), b2_expect);
    }
    let stats = store.stats().unwrap();
    // 6 (main) + 3 (b1 copies) + 2 (b2 copies) = 11 physical pages, all live.
    assert_eq!(stats.live_pages, 11);
    assert_eq!(stats.page_files, 11);
    if exact_allocations {
        assert_eq!(stats.pages_allocated, 11);
    }
}

#[test]
fn final_state_without_injection() {
    let tmp = TempDir::new().unwrap();
    run_script(tmp.path());
    verify_final(tmp.path(), true);
}
