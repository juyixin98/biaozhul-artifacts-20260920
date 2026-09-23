#![allow(clippy::needless_return)]

use cow_snapshot::store::Store;
use tempfile::TempDir;

fn fresh() -> (TempDir, Store) {
    let dir = TempDir::new().unwrap();
    let store = Store::open(dir.path()).unwrap();
    (dir, store)
}

#[test]
fn empty_read_is_none_and_bounds_checked() {
    let (_d, s) = fresh();
    s.create_branch("main", None).unwrap();
    assert_eq!(s.read_page("main", 0).unwrap(), None);
    assert_eq!(s.read_page("main", 63).unwrap(), None);
    assert!(s.read_page("main", 64).is_err());
    assert!(s.read_page("nope", 0).is_err());
}

#[test]
fn write_then_read_roundtrip_and_page_size_limits() {
    let (_d, s) = fresh();
    s.create_branch("main", None).unwrap();

    let data = b"hello page zero";
    let out = s.write_page("main", 0, data).unwrap();
    assert_eq!(out.copied_pages, 2, "a page write copies leaf + new root");
    assert_eq!(s.read_page("main", 0).unwrap().unwrap(), data);

    // overwriting keeps the snapshot content consistent
    let data2 = vec![7u8; 4096];
    s.write_page("main", 0, &data2).unwrap();
    assert_eq!(s.read_page("main", 0).unwrap().unwrap(), data2);

    assert!(s.write_page("main", 0, &[]).is_err());
    assert!(s.write_page("main", 0, &vec![0u8; 4097]).is_err());
}

#[test]
fn reopen_persists_state() {
    let dir = TempDir::new().unwrap();
    {
        let s = Store::open(dir.path()).unwrap();
        s.create_branch("main", None).unwrap();
        s.write_page("main", 3, b"persist-me").unwrap();
    }
    let s = Store::open(dir.path()).unwrap();
    assert_eq!(s.read_page("main", 3).unwrap().unwrap(), b"persist-me");
    assert_eq!(s.read_page("main", 0).unwrap(), None);
}

#[test]
fn branching_shares_leaf_pages_and_copies_no_leaf_data() {
    let (_d, s) = fresh();
    s.create_branch("main", None).unwrap();
    s.write_page("main", 0, b"A0").unwrap();
    s.write_page("main", 1, b"A1").unwrap();

    let out = s.create_branch("child", Some("main")).unwrap();
    assert_eq!(out.copied_pages, 1, "branching copies only the root page");

    // identical backing page ids => shared physical pages, zero leaf copies
    assert_eq!(
        s.page_id_at("main", 0).unwrap(),
        s.page_id_at("child", 0).unwrap()
    );
    assert_eq!(
        s.page_id_at("main", 1).unwrap(),
        s.page_id_at("child", 1).unwrap()
    );

    let st = s.stats().unwrap();
    // 3 roots (main, main@w1, main@w2 are superseded and freed; live: main root + child root)
    // + 2 shared leaves = 4 live physical pages
    assert_eq!(st.live_page_count, 4);
    let leaf = s.page_id_at("main", 0).unwrap();
    assert_eq!(
        st.refcounts[&leaf], 2,
        "shared leaf referenced by both roots"
    );
}

#[test]
fn overwrite_isolated_to_branch() {
    let (_d, s) = fresh();
    s.create_branch("main", None).unwrap();
    s.write_page("main", 0, b"orig").unwrap();
    s.create_branch("child", Some("main")).unwrap();

    s.write_page("child", 0, b"child-new").unwrap();
    assert_eq!(s.read_page("child", 0).unwrap().unwrap(), b"child-new");
    assert_eq!(s.read_page("main", 0).unwrap().unwrap(), b"orig");
    assert_ne!(
        s.page_id_at("main", 0).unwrap(),
        s.page_id_at("child", 0).unwrap()
    );
}

#[test]
fn exact_copy_accounting_simple_chain() {
    let (_d, s) = fresh();
    s.create_branch("main", None).unwrap(); // 1 root
    for i in 0..4usize {
        s.write_page("main", i, format!("p{i}").as_bytes()).unwrap(); // 2 pages each
    }
    // 1 branch root + 4*2 = 9 pages minted; old roots were freed but ids are not reused
    let st = s.stats().unwrap();
    assert_eq!(st.pages_minted_total, 9);
    assert_eq!(st.live_page_count, 5, "current root + 4 leaves");
}
