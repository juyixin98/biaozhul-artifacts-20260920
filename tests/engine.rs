//! Engine-level acceptance test:
//! two-level branches, interleaved overwrites, branch deletion,
//! verification of snapshot contents, live page set and copy counts.

use cow_snapstore::Store;
use tempfile::TempDir;

fn page(tag: &str, i: u64) -> Vec<u8> {
    format!("{tag}:{i}").into_bytes()
}

fn read(store: &Store, snap: &str, i: u64) -> Vec<u8> {
    store.read_page(snap, i).unwrap_or_else(|e| panic!("read {snap}/{i}: {e}"))
}

#[test]
fn two_level_branch_interleaved_overwrite_delete() {
    let tmp = TempDir::new().unwrap();
    let store = Store::open(tmp.path()).unwrap();

    // Level 0: main with 8 pages.
    store.create_snapshot("main", None).unwrap();
    for i in 0..8 {
        store.write_page("main", i, &page("main-v1", i)).unwrap();
    }

    // Two levels of branches — no data copied.
    store.create_snapshot("b1", Some("main")).unwrap();
    store.create_snapshot("b2", Some("b1")).unwrap();
    assert_eq!(store.stats().unwrap().pages_allocated, 8, "branching must not copy pages");

    // Interleaved overwrites across all three snapshots.
    for i in [0, 2, 4] {
        store.write_page("main", i, &page("main-v2", i)).unwrap();
    }
    for i in [1, 3, 5] {
        store.write_page("b1", i, &page("b1", i)).unwrap();
    }
    for i in [6, 7] {
        store.write_page("b2", i, &page("b2", i)).unwrap();
    }
    store.write_page("main", 6, &page("main-v2", 6)).unwrap();

    // Every COW write allocates exactly one page: 8 + 3 + 3 + 2 + 1.
    assert_eq!(store.stats().unwrap().pages_allocated, 17);

    // Delete the middle branch.
    store.delete_snapshot("b1").unwrap();

    // main: overwritten at 0,2,4,6; original elsewhere.
    for i in 0..8 {
        let expect = if [0, 2, 4, 6].contains(&i) { page("main-v2", i) } else { page("main-v1", i) };
        assert_eq!(read(&store, "main", i), expect, "main page {i}");
    }
    // b2: branched from b1 BEFORE b1's overwrites, so it sees main-v1 at 0..=5,
    // and its own writes at 6,7. Deleting b1 must not affect b2.
    for i in 0..8 {
        let expect = if [6, 7].contains(&i) { page("b2", i) } else { page("main-v1", i) };
        assert_eq!(read(&store, "b2", i), expect, "b2 page {i}");
    }

    // Live set = union of surviving manifests:
    //   originals p1,p3,p5,p7 (main) + p0..p5 (b2) + main's 4 copies + b2's 2
    //   copies = 13. Dead: b1's 3 private copies + original page 6 (main and
    //   b2 both overwrote it, b1 deleted) = 4 orphans reclaimed by GC.
    let stats = store.stats().unwrap();
    assert_eq!(stats.live_pages, 13);
    assert_eq!(stats.orphan_pages, 4);
    let removed = store.gc().unwrap();
    assert_eq!(removed.len(), 4);
    let stats = store.stats().unwrap();
    assert_eq!(stats.page_files, 13);
    assert_eq!(stats.orphan_pages, 0);

    // Sharing breakdown. main shares p1,p3,p5 with b2 (p7 is only main's,
    // since b2 overwrote index 7).
    let main_info = store.snapshot_info("main").unwrap();
    assert_eq!(main_info.private_pages, 5); // 4 v2 copies + untouched p7
    assert_eq!(main_info.shared_pages, 3); // p1,p3,p5 shared with b2
    let b2_info = store.snapshot_info("b2").unwrap();
    assert_eq!(b2_info.private_pages, 5); // p0,p2,p4 + its 2 copies
    assert_eq!(b2_info.shared_pages, 3); // p1,p3,p5 shared with main

    // Persistence: reopen and re-verify contents and stats.
    drop(store);
    let store = Store::open(tmp.path()).unwrap();
    for i in 0..8 {
        let expect = if [0, 2, 4, 6].contains(&i) { page("main-v2", i) } else { page("main-v1", i) };
        assert_eq!(read(&store, "main", i), expect, "main page {i} after reopen");
    }
    for i in 0..8 {
        let expect = if [6, 7].contains(&i) { page("b2", i) } else { page("main-v1", i) };
        assert_eq!(read(&store, "b2", i), expect, "b2 page {i} after reopen");
    }
    assert_eq!(store.stats().unwrap().live_pages, 13);
    assert_eq!(store.stats().unwrap().pages_allocated, 17);
}

#[test]
fn errors_and_edge_cases() {
    let tmp = TempDir::new().unwrap();
    let store = Store::open(tmp.path()).unwrap();

    // Unknown snapshots.
    assert!(store.read_page("nope", 0).is_err());
    assert!(store.write_page("nope", 0, b"x").is_err());
    assert!(store.delete_snapshot("nope").is_err());
    assert!(store.create_snapshot("b", Some("nope")).is_err());

    store.create_snapshot("s", None).unwrap();
    assert!(store.create_snapshot("s", None).is_err(), "duplicate name");
    assert!(store.create_snapshot("bad/name", None).is_err(), "invalid name");

    // Hole in the page table reads as not-found.
    store.write_page("s", 5, b"five").unwrap();
    assert!(store.read_page("s", 4).is_err());
    assert_eq!(store.read_page("s", 5).unwrap(), b"five");

    // Oversized payload rejected.
    assert!(store.write_page("s", 0, &vec![0u8; cow_snapstore::PAGE_SIZE + 1]).is_err());

    // Full-size page round-trips exactly.
    let big: Vec<u8> = (0..cow_snapstore::PAGE_SIZE).map(|i| (i % 251) as u8).collect();
    store.write_page("s", 6, &big).unwrap();
    assert_eq!(store.read_page("s", 6).unwrap(), big);
}
