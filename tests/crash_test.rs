//! Per-fault-point crash injection using a REAL process death.
//!
//! For every crash point of write / branch / delete we:
//!   1. arm the point, run the operation in a `crash-driver` child with COW_CRASH_KILL=1
//!      (the child calls exit(37) at the point; only fsynced state survives)
//!   2. reopen the store in this process, which runs crash recovery
//!   3. assert: open succeeds, the operation is either fully applied or fully absent
//!      according to where the commit point (MANIFEST rename) sits, every previously
//!      committed snapshot is byte-exact, on-disk pages == reachable pages, and
//!      refcounts equal an independent root walk
//!   4. disarm and replay the operation, then assert invariants still hold

use std::collections::{BTreeMap, BTreeSet};
use std::process::{Command, Stdio};

use cow_snapshot::store::Store;
use tempfile::TempDir;

const DRIVER: &str = env!("CARGO_BIN_EXE_crash-driver");

type Model = BTreeMap<(String, usize), Vec<u8>>;

fn driver(dir: &std::path::Path) -> Command {
    let mut c = Command::new(DRIVER);
    c.arg("--dir")
        .arg(dir)
        .env("COW_CRASH_KILL", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    c
}

/// Returns true if the child died with exit code 37. If the point is never reached the
/// child simply succeeds (this is legitimate for free-points that have nothing to free).
fn run_expect_crash(dir: &std::path::Path, args: &[&str]) -> bool {
    let out = driver(dir).args(args).output().expect("spawn driver");
    if out.status.code() == Some(37) {
        return true;
    }
    assert!(
        out.status.success(),
        "driver {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    false
}

fn run_ok(dir: &std::path::Path, args: &[&str]) {
    let out = driver(dir).args(args).output().expect("spawn driver");
    assert!(
        out.status.success(),
        "driver {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

fn arm(dir: &std::path::Path, point: &str) {
    let s = Store::open(dir).unwrap();
    s.set_fault(Some(point)).unwrap();
}

fn model_from_store(s: &Store) -> Model {
    let mut m = Model::new();
    for b in s.list_branches() {
        for i in 0..s.slots() {
            if let Some(data) = s.read_page(&b.name, i).unwrap() {
                m.insert((b.name.clone(), i), data);
            }
        }
    }
    m
}

fn assert_model(s: &Store, m: &Model) {
    let branches: BTreeSet<String> = s.list_branches().into_iter().map(|b| b.name).collect();
    let model_branches: BTreeSet<String> = m.keys().map(|(b, _)| b.clone()).collect();
    assert_eq!(branches, model_branches, "branch set diverged from model");
    for ((b, i), want) in m {
        let got = s
            .read_page(b, *i)
            .unwrap()
            .unwrap_or_else(|| panic!("page {b}[{i}] vanished after recovery"));
        assert_eq!(&got, want, "torn content for {b}[{i}]");
    }
}

/// Full structural invariant: files on disk == live pages, rc files == walk counts.
fn assert_invariants(dir: &std::path::Path, s: &Store) {
    let st = s.stats().unwrap();
    let live: BTreeSet<u64> = st.live_pages.iter().copied().collect();

    let mut walk_counts: BTreeMap<u64, u32> = BTreeMap::new();
    let mut walk_live = BTreeSet::new();
    for b in &st.branches {
        walk_live.insert(b.root);
        for i in 0..st.slots {
            let pid = s.page_id_at(&b.name, i).unwrap();
            if pid != 0 {
                walk_live.insert(pid);
                *walk_counts.entry(pid).or_insert(0) += 1;
            }
        }
    }
    assert_eq!(live, walk_live, "stats live set != root walk");

    for pid in &live {
        assert!(
            dir.join("data").join(format!("{pid}.page")).exists(),
            "live page {pid} missing on disk"
        );
    }
    for ent in std::fs::read_dir(dir.join("data")).unwrap() {
        let n = ent.unwrap().file_name().to_string_lossy().to_string();
        if let Some(stem) = n.strip_suffix(".page") {
            let pid: u64 = stem.parse().unwrap();
            assert!(live.contains(&pid), "orphan page {pid} survived recovery");
        } else {
            assert!(!n.starts_with(".tmp"), "temp file leaked: {n}");
        }
    }
    for (pid, want) in walk_counts {
        assert_eq!(
            st.refcounts.get(&pid).copied(),
            Some(want),
            "rc[{pid}] wrong"
        );
    }
    for b in &st.branches {
        assert_eq!(st.refcounts.get(&b.root).copied(), Some(1), "root rc");
    }
    for ent in std::fs::read_dir(dir.join("refcounts")).unwrap() {
        let n = ent.unwrap().file_name().to_string_lossy().to_string();
        let pid: u64 = n.strip_suffix(".rc").unwrap().parse().unwrap();
        assert!(st.refcounts.contains_key(&pid), "stale rc file {n}");
    }
}

/// Scenario: main(0..=3), child branched at main with slot 0 and 2 diverged,
/// grandchild at child. Both shared and private leaves exist.
fn setup(dir: &std::path::Path) {
    let s = Store::open(dir).unwrap();
    s.create_branch("main", None).unwrap();
    for i in 0..4usize {
        s.write_page("main", i, format!("base-{i}").as_bytes())
            .unwrap();
    }
    s.create_branch("child", Some("main")).unwrap();
    s.write_page("child", 0, b"child-0").unwrap();
    s.write_page("child", 2, b"child-2").unwrap();
    s.create_branch("grandchild", Some("child")).unwrap();
}

fn write_pre_commit() -> &'static [&'static str] {
    &[
        "before_page",
        "after_page",
        "before_setrc",
        "after_setrc",
        "before_root",
        "after_root",
        "before_rootrc",
        "after_rootrc",
        "before_manifest",
    ]
}

fn write_post_commit() -> &'static [&'static str] {
    &["after_manifest", "before_free", "after_free"]
}

fn branch_pre_commit() -> &'static [&'static str] {
    &[
        "before_root",
        "after_root",
        "before_rootrc",
        "after_rootrc",
        "before_share",
        "after_share",
        "before_manifest",
    ]
}

#[test]
fn crash_during_write_at_every_point() {
    let all: Vec<&str> = write_pre_commit()
        .iter()
        .chain(write_post_commit())
        .copied()
        .collect();
    for point in all {
        let dir = TempDir::new().unwrap();
        setup(dir.path());
        let before = {
            let s = Store::open(dir.path()).unwrap();
            model_from_store(&s)
        };
        let post_commit = write_post_commit().contains(&point);

        arm(dir.path(), point);
        let crashed = run_expect_crash(dir.path(), &["write", "child", "1", "new-c1"]);

        let s = Store::open(dir.path()); // runs recovery
        assert!(s.is_ok(), "reopen after {point} failed: {:?}", s.err());
        let s = s.unwrap();
        assert_invariants(dir.path(), &s);

        let mut expect = before.clone();
        if post_commit && crashed {
            expect.insert(("child".into(), 1), b"new-c1".to_vec());
        }
        assert_model(&s, &expect);
        eprintln!("write @ {point:<18} crashed={crashed} post_commit={post_commit} ok");

        // replay with fault disarmed: idempotent convergence to the new value
        drop(s);
        Store::open(dir.path()).unwrap().set_fault(None).unwrap();
        run_ok(dir.path(), &["write", "child", "1", "new-c1"]);
        let s = Store::open(dir.path()).unwrap();
        let mut after = before;
        after.insert(("child".into(), 1), b"new-c1".to_vec());
        assert_model(&s, &after);
        assert_invariants(dir.path(), &s);
    }
}

#[test]
fn crash_overwriting_a_three_way_shared_leaf() {
    // child slot 3 is shared with main AND grandchild; siblings must keep their page.
    let all: Vec<&str> = write_pre_commit()
        .iter()
        .chain(write_post_commit())
        .copied()
        .collect();
    for point in all {
        let dir = TempDir::new().unwrap();
        setup(dir.path());
        arm(dir.path(), point);
        let crashed = run_expect_crash(dir.path(), &["write", "child", "3", "child-c3"]);
        let _ = crashed;

        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);

        Store::open(dir.path()).unwrap().set_fault(None).unwrap();
        drop(s);
        run_ok(dir.path(), &["write", "child", "3", "child-c3"]);
        let s = Store::open(dir.path()).unwrap();
        assert_eq!(s.read_page("child", 3).unwrap().unwrap(), b"child-c3");
        assert_eq!(s.read_page("main", 3).unwrap().unwrap(), b"base-3");
        assert_eq!(s.read_page("grandchild", 3).unwrap().unwrap(), b"base-3");
        assert_invariants(dir.path(), &s);
    }
}

#[test]
fn crash_during_branch_at_every_point() {
    for point in branch_pre_commit().iter().chain(["after_manifest"].iter()) {
        let dir = TempDir::new().unwrap();
        setup(dir.path());
        let before = {
            let s = Store::open(dir.path()).unwrap();
            model_from_store(&s)
        };
        let post_commit = *point == "after_manifest";
        arm(dir.path(), point);
        let crashed = run_expect_crash(dir.path(), &["branch", "g2", "grandchild"]);

        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);
        if post_commit && crashed {
            assert!(s.list_branches().iter().any(|b| b.name == "g2"));
            assert_eq!(s.read_page("g2", 0).unwrap().unwrap(), b"child-0");
            assert_eq!(s.read_page("g2", 1).unwrap().unwrap(), b"base-1");
        } else {
            assert!(
                s.list_branches().iter().all(|b| b.name != "g2"),
                "phantom branch after {point}"
            );
            assert_model(&s, &before);
        }
        eprintln!("branch @ {point:<18} crashed={crashed} ok");

        drop(s);
        Store::open(dir.path()).unwrap().set_fault(None).unwrap();
        if !(post_commit && crashed) {
            run_ok(dir.path(), &["branch", "g2", "grandchild"]);
        }
        let s = Store::open(dir.path()).unwrap();
        assert_eq!(s.read_page("g2", 0).unwrap().unwrap(), b"child-0");
        assert_eq!(s.read_page("g2", 1).unwrap().unwrap(), b"base-1");
        assert_invariants(dir.path(), &s);
    }
}

#[test]
fn crash_during_delete_at_every_point() {
    // pre-commit vs post-commit classification for delete
    let pre = ["before_manifest_del"];
    let post = [
        "after_manifest_del",
        "before_dec",
        "before_free",
        "after_free",
    ];
    for point in pre.iter().chain(post.iter()) {
        let dir = TempDir::new().unwrap();
        setup(dir.path());
        let before = {
            let s = Store::open(dir.path()).unwrap();
            s.create_branch("victim", Some("main")).unwrap();
            s.write_page("victim", 0, b"victim-only").unwrap();
            model_from_store(&s)
        };
        let post_commit = post.contains(point);
        arm(dir.path(), point);
        let crashed = run_expect_crash(dir.path(), &["delete", "victim"]);

        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);
        let exists = s.list_branches().iter().any(|b| b.name == "victim");
        if post_commit && crashed {
            assert!(!exists, "delete should be visible after commit at {point}");
            let mut m = before.clone();
            m.retain(|(b, _), _| b != "victim");
            assert_model(&s, &m);
            assert_eq!(s.read_page("main", 0).unwrap().unwrap(), b"base-0");
        } else {
            assert!(exists, "delete must not appear before commit at {point}");
            assert_model(&s, &before);
        }
        eprintln!("delete @ {point:<20} crashed={crashed} ok");

        drop(s);
        Store::open(dir.path()).unwrap().set_fault(None).unwrap();
        if exists {
            run_ok(dir.path(), &["delete", "victim"]);
        }
        let s = Store::open(dir.path()).unwrap();
        assert!(s.list_branches().iter().all(|b| b.name != "victim"));
        assert_eq!(s.read_page("main", 0).unwrap().unwrap(), b"base-0");
        assert_invariants(dir.path(), &s);
    }
}

#[test]
fn repeated_crashes_then_convergence() {
    let dir = TempDir::new().unwrap();
    setup(dir.path());
    let points = [
        "before_page",
        "after_root",
        "before_manifest",
        "after_manifest",
        "before_free",
    ];
    for (round, point) in points.iter().enumerate() {
        arm(dir.path(), point);
        let idx = 1 + (round % 3);
        let payload = format!("round-{round}");
        let _ = run_expect_crash(dir.path(), &["write", "main", &idx.to_string(), &payload]);

        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);
        drop(s);
        Store::open(dir.path()).unwrap().set_fault(None).unwrap();
        run_ok(dir.path(), &["write", "main", &idx.to_string(), &payload]);
    }
    let s = Store::open(dir.path()).unwrap();
    assert_eq!(s.read_page("main", 1).unwrap().unwrap(), b"round-3");
    assert_eq!(s.read_page("main", 2).unwrap().unwrap(), b"round-4");
    assert_eq!(s.read_page("main", 3).unwrap().unwrap(), b"round-2");
    // other branches never saw main's later writes (snapshot isolation across crashes)
    assert_eq!(s.read_page("child", 1).unwrap().unwrap(), b"base-1");
    assert_invariants(dir.path(), &s);
}

#[test]
fn crash_on_each_shared_page_during_branch() {
    // "Per-page" fault injection: branching bumps refcounts one leaf at a time. Arm
    // before_share to fire on the n-th leaf (n = 1..=4) so each shared page is individually
    // killed mid-branch; recovery must leave a consistent store and the branch retry must
    // end with every shared leaf at the correct (incremented) count.
    for nth in 1u64..=4 {
        let dir = TempDir::new().unwrap();
        {
            let s = Store::open(dir.path()).unwrap();
            s.create_branch("main", None).unwrap();
            for i in 0..4usize {
                s.write_page("main", i, format!("p{i}").as_bytes()).unwrap();
            }
            s.set_fault(Some(&format!("before_share:{nth}"))).unwrap();
        }

        let crashed = run_expect_crash(dir.path(), &["branch", "child", "main"]);
        assert!(crashed, "before_share:{nth} should fire");

        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);
        assert!(s.list_branches().iter().all(|b| b.name != "child"));

        // retry with fault cleared
        s.set_fault(None).unwrap();
        drop(s);
        run_ok(dir.path(), &["branch", "child", "main"]);
        let s = Store::open(dir.path()).unwrap();
        assert_invariants(dir.path(), &s);
        for i in 0..4 {
            assert_eq!(
                s.read_page("child", i).unwrap().unwrap(),
                format!("p{i}").as_bytes()
            );
            // each leaf now referenced by both roots
            let pid = s.page_id_at("main", i).unwrap();
            assert_eq!(s.stats().unwrap().refcounts[&pid], 2, "nth={nth} slot {i}");
        }
    }
}

#[test]
fn orphan_from_pre_commit_crash_is_reclaimed_on_later_open() {
    // crash after the leaf/root files were written but before commit; reopen reclaims them.
    let dir = TempDir::new().unwrap();
    setup(dir.path());
    arm(dir.path(), "after_rootrc");
    run_expect_crash(dir.path(), &["write", "main", "0", "will-be-orphan"]);
    let s = Store::open(dir.path()).unwrap();
    assert_eq!(s.read_page("main", 0).unwrap().unwrap(), b"base-0");
    assert_invariants(dir.path(), &s);
    // a second, fault-free reopen must not change anything
    drop(s);
    let s = Store::open(dir.path()).unwrap();
    assert_invariants(dir.path(), &s);
}
