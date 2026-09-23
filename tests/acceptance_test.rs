//! Acceptance scenario: build a TWO-LEVEL branch tree, interleave overwrites across the
//! branches, delete a middle branch, and verify every snapshot's contents, the live page
//! set, leaf refcounts, and the number of physical pages actually copied.

use std::collections::{BTreeMap, BTreeSet};

use cow_snapshot::store::Store;
use tempfile::TempDir;

/// expected[(branch, index)] = exact bytes
type Model = BTreeMap<(String, usize), Vec<u8>>;

fn expect_contents(s: &Store, m: &Model) {
    for ((b, idx), want) in m {
        let got = s
            .read_page(b, *idx)
            .unwrap()
            .unwrap_or_else(|| panic!("missing {b}[{idx}]"));
        assert_eq!(&got, want, "content mismatch for {b}[{idx}]");
    }
}

fn current_live_set(s: &Store) -> BTreeSet<u64> {
    let st = s.stats().unwrap();
    let from_stats: BTreeSet<u64> = st.live_pages.iter().copied().collect();

    // independently rebuild from branch roots + page_id_at
    let branches: Vec<String> = s.list_branches().into_iter().map(|b| b.name).collect();
    let mut manual: BTreeSet<u64> = BTreeSet::new();
    for b in &branches {
        let info = s
            .list_branches()
            .into_iter()
            .find(|x| &x.name == b)
            .unwrap();
        manual.insert(info.root);
        for i in 0..s.slots() {
            let pid = s.page_id_at(b, i).unwrap();
            if pid != 0 {
                manual.insert(pid);
            }
        }
    }
    assert_eq!(
        from_stats, manual,
        "stats live set disagrees with root walk"
    );
    manual
}

fn expect_refcounts(s: &Store) {
    let branches: Vec<String> = s.list_branches().into_iter().map(|b| b.name).collect();
    let mut counts: BTreeMap<u64, u32> = BTreeMap::new();
    for b in &branches {
        for i in 0..s.slots() {
            let pid = s.page_id_at(b, i).unwrap();
            if pid != 0 {
                *counts.entry(pid).or_insert(0) += 1;
            }
        }
    }
    let st = s.stats().unwrap();
    for (pid, want) in counts {
        assert_eq!(
            st.refcounts.get(&pid).copied().unwrap_or(0),
            want,
            "refcount of leaf {pid} should be {want}"
        );
    }
    // root pages each have rc 1
    for b in st.branches {
        assert_eq!(st.refcounts.get(&b.root).copied().unwrap_or(0), 1);
    }
}

fn body(tag: &str) -> Vec<u8> {
    format!("<<<{tag}>>>").into_bytes()
}

#[test]
fn two_level_branch_tree_interleaved_overwrites_and_delete() {
    let dir = TempDir::new().unwrap();
    let s = Store::open(dir.path()).unwrap();
    let mut model: Model = BTreeMap::new();
    let mut leaf_copies = 0u64;
    let mut root_copies = 0u64;

    // --- base snapshot with 5 pages -------------------------------------------
    s.create_branch("main", None).unwrap();
    root_copies += 1;
    for i in 0..5usize {
        let d = body(&format!("base-{i}"));
        s.write_page("main", i, &d).unwrap();
        leaf_copies += 1;
        root_copies += 1;
        model.insert(("main".into(), i), d);
    }

    // --- two-level branch tree: main -> child -> grandchild --------------------
    s.create_branch("child", Some("main")).unwrap();
    root_copies += 1;
    s.create_branch("grandchild", Some("child")).unwrap();
    root_copies += 1;
    for i in 0..5 {
        model.insert(("child".into(), i), model[&("main".into(), i)].clone());
        model.insert(
            ("grandchild".into(), i),
            model[&("child".into(), i)].clone(),
        );
    }
    expect_contents(&s, &model);
    expect_refcounts(&s);

    // base leaves are shared three ways before any divergence
    let base_leaf = s.page_id_at("main", 0).unwrap();
    assert_eq!(s.stats().unwrap().refcounts[&base_leaf], 3);

    // --- interleaved overwrites ------------------------------------------------
    let steps: Vec<(&str, usize, &str)> = vec![
        ("main", 1, "main-w1"),
        ("child", 2, "child-w2"),
        ("grandchild", 3, "gc-w3"),
        ("child", 1, "child-w1"),
        ("main", 0, "main-w0"),
        ("grandchild", 0, "gc-w0"),
        ("main", 4, "main-w4"),
        ("child", 2, "child-w2b"),
    ];
    for (b, idx, tag) in &steps {
        let d = body(tag);
        let out = s.write_page(b, *idx, &d).unwrap();
        assert_eq!(out.copied_pages, 2);
        leaf_copies += 1;
        root_copies += 1;
        model.insert((b.to_string(), *idx), d);
    }
    expect_contents(&s, &model);
    expect_refcounts(&s);

    // Every slot 0..=4 was overwritten by at least one branch, so no leaf is shared by
    // all three; but pair-wise sharing of untouched leaves survives:
    //   slot 2 diverged only in child  -> main == grandchild (both hold base-2)
    //   slot 3 diverged only in grandchild -> main == child (both hold base-3)
    //   slot 4 diverged only in main   -> child == grandchild (both hold base-4)
    assert_eq!(
        s.page_id_at("main", 2).unwrap(),
        s.page_id_at("grandchild", 2).unwrap()
    );
    assert_ne!(
        s.page_id_at("child", 2).unwrap(),
        s.page_id_at("main", 2).unwrap()
    );
    assert_eq!(
        s.page_id_at("main", 3).unwrap(),
        s.page_id_at("child", 3).unwrap()
    );
    assert_ne!(
        s.page_id_at("grandchild", 3).unwrap(),
        s.page_id_at("main", 3).unwrap()
    );
    assert_eq!(
        s.page_id_at("child", 4).unwrap(),
        s.page_id_at("grandchild", 4).unwrap()
    );
    assert_ne!(
        s.page_id_at("main", 4).unwrap(),
        s.page_id_at("child", 4).unwrap()
    );
    // diverged pages differ physically
    assert_ne!(
        s.page_id_at("main", 0).unwrap(),
        s.page_id_at("grandchild", 0).unwrap()
    );

    let live = current_live_set(&s);
    let st = s.stats().unwrap();
    assert_eq!(
        st.pages_minted_total,
        leaf_copies + root_copies,
        "every minted page id is one physical copy"
    );
    // 3 current roots + 12 distinct leaves:
    //   main: {mw0,mw1,base2,base3,mw4}
    //   child adds {base0,cw1,cw2b,base4} (base3 shared with main)
    //   grandchild adds {gcw0,base1,gcw3} (base2 via main, base4 via child)
    let mut distinct_leaves: BTreeSet<u64> = BTreeSet::new();
    for b in ["main", "child", "grandchild"] {
        for i in 0..5 {
            distinct_leaves.insert(s.page_id_at(b, i).unwrap());
        }
    }
    assert_eq!(distinct_leaves.len(), 12);
    assert_eq!(live.len(), 3 + distinct_leaves.len());

    // cost comparison vs. naive whole-snapshot copy
    let naive_copies: u64 =
        steps.len() as u64 * 5 /*pages*/ + 2 /*branch copies x5 pages each*/ * 5;
    let cow_copies = leaf_copies + root_copies;
    assert!(cow_copies < naive_copies);
    eprintln!("COW copies = {cow_copies}, naive whole-snapshot copies = {naive_copies}");

    // --- delete the middle branch; shared pages must survive -------------------
    s.delete_branch("child").unwrap();
    model.retain(|(b, _), _| b != "child");
    expect_contents(&s, &model);
    assert!(s.read_page("child", 0).is_err());
    expect_refcounts(&s);

    // grandchild keeps pages it shared with the deleted child (slots 2 and 4 are base content)
    assert_eq!(
        s.read_page("grandchild", 2).unwrap().unwrap(),
        body("base-2")
    );
    assert_eq!(
        s.read_page("grandchild", 4).unwrap().unwrap(),
        body("base-4")
    );

    let live_after = current_live_set(&s);
    let st = s.stats().unwrap();
    let mut distinct_after: BTreeSet<u64> = BTreeSet::new();
    for b in ["main", "grandchild"] {
        for i in 0..5 {
            distinct_after.insert(s.page_id_at(b, i).unwrap());
        }
    }
    assert_eq!(live_after.len(), 2 + distinct_after.len());
    assert_eq!(st.live_page_count, live_after.len());

    // pages private to child are gone from disk; shared/shared-with-grandchild survive
    for pid in &live {
        let on_disk = dir.path().join("data").join(format!("{pid}.page")).exists();
        assert_eq!(
            on_disk,
            live_after.contains(pid),
            "page {pid} liveness wrong"
        );
    }

    // deleting the other branches leaves an empty (but valid) store
    s.delete_branch("grandchild").unwrap();
    s.delete_branch("main").unwrap();
    let st = s.stats().unwrap();
    assert_eq!(st.live_page_count, 0, "all pages reclaimed");
    assert!(st.refcounts.is_empty());
    // and it reopens cleanly with no orphans
    drop(s);
    let s2 = Store::open(dir.path()).unwrap();
    assert_eq!(s2.stats().unwrap().live_page_count, 0);
}
