//! Crash-recovery tests.
//!
//! Each case runs the `crash-runner` binary in a *separate process* with a
//! hook that calls `_exit(9)` at a chosen durability point, then reopens the
//! index file in this process and asserts:
//!
//! 1. open succeeds and reports exactly one recovered intent;
//! 2. all pre-crash data is intact and the post-crash insert is either fully
//!    applied or fully absent (atomicity);
//! 3. the structure is usable afterwards and a full key/value scan matches an
//!    in-memory model;
//! 4. structural invariants (slot refs == 2^(gd-ld), directory consistent)
//!    hold after recovery.
use ext_hash_index::{Config, HashKind, Index, Stats};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

fn tmp_dir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "ext-hash-crash-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn runner() -> String {
    let exe = env!("CARGO_BIN_EXE_crash-runner");
    exe.to_string()
}

struct RunSpec<'a> {
    file: &'a Path,
    op: &'a str,
    key: &'a str,
    value: &'a str,
    crash: Option<&'a str>,
    capacity: usize,
    hash: &'a str,
    expect_ok: bool,
}

fn run_crash(s: RunSpec) -> std::process::Output {
    let mut c = Command::new(runner());
    c.args([
        "--file",
        s.file.to_str().unwrap(),
        "--op",
        s.op,
        "--key",
        s.key,
        "--value",
        s.value,
        "--capacity",
        &s.capacity.to_string(),
        "--hash",
        s.hash,
    ]);
    if let Some(p) = s.crash {
        c.args(["--crash", p]);
    } else {
        c.args(["--crash", "__none__"]);
    }
    let out = c.output().expect("crash-runner failed to spawn");
    if s.expect_ok && out.status.code() != Some(if s.crash.is_some() { 9 } else { 0 }) {
        panic!(
            "unexpected runner status {:?}\nstdout: {}\nstderr: {}",
            out.status,
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
    }
    out
}

/// Assert the structural invariants of an extendible hash directory.
fn assert_invariants(st: &Stats) {
    use std::collections::BTreeMap;
    let gd = st.global_depth;
    let mut refs: BTreeMap<u32, usize> = BTreeMap::new();
    // slot_refs reported per bucket must sum to directory size.
    let mut sum = 0usize;
    for b in &st.buckets {
        assert!(b.local_depth <= gd, "bucket local_depth > global_depth");
        assert_eq!(
            b.slot_refs,
            1usize << (gd - b.local_depth),
            "bad slot_refs for bucket page {}",
            b.page
        );
        sum += b.slot_refs;
        refs.insert(b.page, b.slot_refs);
    }
    assert_eq!(sum, st.directory_slots);
    assert_eq!(st.bucket_count as usize, st.buckets.len());
}

/// Full-scan an index against an ordered in-memory model.
fn assert_matches_model(idx: &mut Index, model: &BTreeMap<String, String>) {
    let st = idx.stats().unwrap();
    assert_invariants(&st);
    let mut on_disk: BTreeMap<String, String> = BTreeMap::new();
    for b in &st.buckets {
        for k in &b.keys {
            let v = idx
                .get(k.as_bytes())
                .unwrap()
                .unwrap_or_else(|| panic!("key {} listed in bucket but unreadable", k));
            on_disk.insert(k.clone(), String::from_utf8(v).unwrap());
        }
    }
    assert_eq!(&on_disk, model, "on-disk contents differ from model");
}

const SPLIT_POINTS: &[&str] = &[
    "split.after_intent",
    "split.after_buckets",
    "split.after_directory",
    "split.after_commit_meta",
];

#[test]
fn recover_split_at_every_point_capacity4() {
    // Keys 0..3 fill the single bucket; key 4 forces a doubling+split.
    for point in SPLIT_POINTS {
        let dir = tmp_dir("split");
        let file = dir.join("i.db");

        // Seed without crashing.
        for k in 0u64..4 {
            run_crash(RunSpec {
                file: &file,
                op: "put",
                key: &k.to_string(),
                value: "v",
                crash: None,
                capacity: 4,
                hash: "u64lowbits",
                expect_ok: true,
            });
        }
        // Crash during split for key 4.
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: "4",
            value: "v",
            crash: Some(point),
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });

        // Recovery in-process.
        let mut idx = Index::open(&file).expect("open after crash must succeed");
        assert_eq!(
            idx.recovered_intent(),
            Some("split"),
            "expected split replay at point {}",
            point
        );
        // Pre-crash keys intact; key 4 absent (crashed before completion).
        for k in 0u64..4 {
            assert_eq!(
                idx.get(k.to_string().as_bytes()).unwrap(),
                Some(b"v".to_vec()),
                "point {}",
                point
            );
        }
        assert_eq!(idx.get(b"4").unwrap(), None, "point {}", point);

        // Structure is healthy and the key now inserts without trouble.
        idx.put(b"4", b"v").unwrap();
        let mut model: BTreeMap<String, String> = BTreeMap::new();
        for k in 0u64..5 {
            model.insert(k.to_string(), "v".into());
        }
        assert_matches_model(&mut idx, &model);
    }
}

#[test]
fn crash_after_reservation_leaves_consistent_no_intent() {
    // Crash between the reservation header (pages popped/bumped, fsynced) and
    // the intent header. The insert must be lost, there must be NO intent to
    // replay, and the index stays fully consistent; the reserved pages are a
    // bounded leak (unreachable, never reused until freed — here simply
    // abandoned, which is safe).
    let dir = tmp_dir("reserve");
    let file = dir.join("i.db");
    for k in 0u64..4 {
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: &k.to_string(),
            value: "v",
            crash: None,
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    run_crash(RunSpec {
        file: &file,
        op: "put",
        key: "4",
        value: "v",
        crash: Some("split.after_reservation"),
        capacity: 4,
        hash: "u64lowbits",
        expect_ok: true,
    });
    let mut idx = Index::open(&file).unwrap();
    assert!(idx.recovered_intent().is_none(), "no intent was persisted");
    for k in 0u64..4 {
        assert_eq!(
            idx.get(k.to_string().as_bytes()).unwrap(),
            Some(b"v".to_vec())
        );
    }
    assert_eq!(idx.get(b"4").unwrap(), None);
    assert_invariants(&idx.stats().unwrap());
    // The key now inserts normally, reusing normal split allocation.
    idx.put(b"4", b"v").unwrap();
    let mut model: BTreeMap<String, String> = BTreeMap::new();
    for k in 0u64..5 {
        model.insert(k.to_string(), "v".into());
    }
    assert_matches_model(&mut idx, &model);
}

#[test]
fn recover_split_without_doubling_shared_bucket() {
    // Capacity 2 with lowbits keys:
    //   insert 0,1 -> bucket full; insert 2 doubles gd 0->1 (even/odd)
    //   insert 3   -> doubles gd 1->2, splitting the odd bucket
    // After 0..3 the directory is:
    //   slot 00 -> {0}   (ld 2)
    //   slot 01 -> {1}   (ld 2)
    //   slot 10 -> {2}   (ld 2)
    //   slot 11 -> {3}   (ld 1, shared by slots 11/... wait: gd=2)
    // Key 4 (low bits 00) lands on bucket {0}, whose ld already equals gd —
    // that would double again. Instead use key 6 (low bits 10) for the {2}
    // bucket: {2} has ld 2 too. To get a genuinely *shared* bucket we need
    // ld < gd. Insert 5 first (low bits 01 -> {1}: {1,5} full, ld 2) then
    // insert 9 (bits 01 too) -> doubling to gd 3. Simpler: directly assert a
    // non-doubling split via keys that leave a shallow bucket. After
    // inserting 0..3 (above), bucket {3} sits at ld 1 (the odd bucket split
    // produced {1} ld2 and {3} ld2 — actually both ld 2). So instead:
    //
    // Use FNV-free, deterministic lowbits sequence with capacity 2 and craft
    // a shallow bucket: gd 2 with keys 0,1,2,3 gives all buckets ld 2.
    // Insert key 8 (bits 00) -> bucket {0} doubles gd 2->3 and splits; after
    // that slots exist pointing at *shared* ld-2 buckets ({1},{2},{3} each
    // mirrored). Inserting key 11 (bits 011) fills bucket {3} whose sibling
    // slots share it at ld 2 < gd 3: splitting it is a NON-doubling split.
    let dir = tmp_dir("splitshared");
    let file = dir.join("i.db");

    for k in [0u64, 1, 2, 3, 8] {
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: &k.to_string(),
            value: "v",
            crash: None,
            capacity: 2,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    {
        let mut idx = Index::open(&file).unwrap();
        let st = idx.stats().unwrap();
        assert_eq!(st.global_depth, 2);
        // A shallow bucket exists: some bucket has slot_refs > 1.
        assert!(
            st.buckets.iter().any(|b| b.slot_refs > 1),
            "expected a shared (ld < gd) bucket before the non-doubling split"
        );
        assert_invariants(&st);
    }
    // 3 (011) and 11 (1011) share slot 011; insert 11 fills the {3} bucket,
    // which splits at ld 2 < gd 3 WITHOUT doubling the directory.
    run_crash(RunSpec {
        file: &file,
        op: "put",
        key: "11",
        value: "v",
        crash: Some("split.after_directory"),
        capacity: 2,
        hash: "u64lowbits",
        expect_ok: true,
    });
    let mut idx = Index::open(&file).unwrap();
    assert_eq!(idx.recovered_intent(), Some("split"));
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 2, "shared-bucket split must not double");
    assert_eq!(
        st.bucket_count, 4,
        "shallow bucket split into two ld2 buckets"
    );
    assert_invariants(&st);
    assert_eq!(idx.get(b"11").unwrap(), None);
    idx.put(b"11", b"v").unwrap();
    let mut model: BTreeMap<String, String> = BTreeMap::new();
    for k in [0u64, 1, 2, 3, 8, 11] {
        model.insert(k.to_string(), "v".into());
    }
    assert_matches_model(&mut idx, &model);
}

const MERGE_POINTS: &[&str] = &[
    "merge.after_intent",
    "merge.after_bucket",
    "merge.after_directory",
    "merge.after_commit_meta",
];

#[test]
fn recover_merge_at_every_point() {
    // Capacity 4, lowbits: 0..4 fill the first bucket; key 4 doubles gd 0->1,
    // splitting into even {0,2,4} and odd {1,3}. Deleting key 4 leaves the
    // even bucket with {0,2} (2 entries): the equal-depth buddy pair {0,2}+
    // {1,3} now fits in one bucket (3 <= 4), so a MERGE runs. The merged
    // bucket is referenced by both directory halves, but shrink does NOT run
    // because the deleted key 4 means the post-merge bucket genuinely is the
    // same for both halves — it actually DOES shrink. To isolate merge from
    // shrink, delete key 4 only: even {0,2} + odd {1,3} = 3 <= 4, and the
    // halves are equal -> merge followed by shrink. We keep the merge crash
    // points before shrink begins, so a merge intent is what is found.
    for point in MERGE_POINTS {
        let dir = tmp_dir("merge");
        let file = dir.join("i.db");
        for k in 0u64..5 {
            run_crash(RunSpec {
                file: &file,
                op: "put",
                key: &k.to_string(),
                value: "v",
                crash: None,
                capacity: 4,
                hash: "u64lowbits",
                expect_ok: true,
            });
        }
        {
            let mut idx = Index::open(&file).unwrap();
            let st = idx.stats().unwrap();
            assert_eq!(st.global_depth, 1);
            assert_invariants(&st);
        }
        run_crash(RunSpec {
            file: &file,
            op: "delete",
            key: "4",
            value: "",
            crash: Some(point),
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });
        let mut idx = Index::open(&file).unwrap();
        assert_eq!(idx.recovered_intent(), Some("merge"), "point {}", point);
        // The delete was durable; merge replay finishes it.
        assert_eq!(idx.get(b"4").unwrap(), None, "point {}", point);
        for k in 0u64..4 {
            assert_eq!(
                idx.get(k.to_string().as_bytes()).unwrap(),
                Some(b"v".to_vec()),
                "point {}",
                point
            );
        }
        assert_invariants(&idx.stats().unwrap());

        // Deleting the remaining keys cascades merges and eventually shrinks.
        for k in 0u64..4 {
            idx.delete(k.to_string().as_bytes()).unwrap();
        }
        let st = idx.stats().unwrap();
        assert_eq!(st.global_depth, 0, "point {}", point);
        assert_eq!(st.bucket_count, 1, "point {}", point);
    }
}

#[test]
fn recover_merge_without_shrink() {
    // Capacity 4, lowbits, keys 0..16: at gd=2 the four buckets are
    //   slot00 {0,4,8,12},  slot01 {1,5,9,13},
    //   slot10 {2,6,10,14}, slot11 {3,7,11,15}.
    // Delete keys 0..7 first (no merges fire: each pair still holds > 4
    // entries total afterwards). State becomes
    //   {8,12}, {9,13}, {10,14}, {11,15}   (all ld2, gd2).
    // Deleting key 8 leaves slot00 bucket {12} (1 entry); its depth-2 buddy
    // slot01 {9,13} (2 entries) yields 1+2 = 3 <= 4 -> they merge into a
    // depth-1 bucket {12,13}. The other two ld2 buckets {10,14}/{11,15}
    // keep the directory halves different, so NO shrink follows.
    let dir = tmp_dir("mergeonly");
    let file = dir.join("i.db");
    for k in 0u64..16 {
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: &k.to_string(),
            value: "v",
            crash: None,
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    for k in 0u64..8 {
        run_crash(RunSpec {
            file: &file,
            op: "delete",
            key: &k.to_string(),
            value: "",
            crash: None,
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    run_crash(RunSpec {
        file: &file,
        op: "delete",
        key: "8",
        value: "",
        crash: Some("merge.after_directory"),
        capacity: 4,
        hash: "u64lowbits",
        expect_ok: true,
    });
    let mut idx = Index::open(&file).unwrap();
    assert_eq!(idx.recovered_intent(), Some("merge"));
    assert_eq!(idx.get(b"8").unwrap(), None);
    let surviving: Vec<u64> = vec![9, 10, 11, 12, 13, 14, 15];
    for k in &surviving {
        assert_eq!(
            idx.get(k.to_string().as_bytes()).unwrap(),
            Some(b"v".to_vec())
        );
    }
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 2, "a partial merge keeps gd=2");
    assert_invariants(&st);
    let mut model: BTreeMap<String, String> = BTreeMap::new();
    for k in &surviving {
        model.insert(k.to_string(), "v".into());
    }
    assert_matches_model(&mut idx, &model);
}

#[test]
fn recover_shrink_mid_flight() {
    // Capacity 2, lowbits. After inserting 0,1,2 the directory is at gd=1:
    //   slot0 -> {0,2}, slot1 -> {1}.
    // Deleting key 2: even bucket becomes {0}; buddy {1} has ld 1 too and
    // {0}+{1}=2 fits, so merge -> one ld0 bucket referenced by both halves.
    // The halves are identical -> shrink gd 1->0 in the same delete. Crash
    // exactly at the shrink barrier (merge crash points don't match it, so
    // the merge itself fully completes and persists).
    let dir = tmp_dir("shrink");
    let file = dir.join("i.db");
    for k in [0u64, 1, 2] {
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: &k.to_string(),
            value: "v",
            crash: None,
            capacity: 2,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    {
        let mut idx = Index::open(&file).unwrap();
        assert_eq!(idx.stats().unwrap().global_depth, 1);
    }
    run_crash(RunSpec {
        file: &file,
        op: "delete",
        key: "2",
        value: "",
        crash: Some("shrink.after_intent"),
        capacity: 2,
        hash: "u64lowbits",
        expect_ok: true,
    });
    let mut idx = Index::open(&file).unwrap();
    assert_eq!(idx.recovered_intent(), Some("shrink"));
    assert_eq!(idx.get(b"2").unwrap(), None);
    assert_eq!(idx.get(b"0").unwrap(), Some(b"v".to_vec()));
    assert_eq!(idx.get(b"1").unwrap(), Some(b"v".to_vec()));
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 0);
    assert_eq!(st.bucket_count, 1);
    assert_invariants(&st);
}

#[test]
fn recovered_index_survives_second_reopen() {
    // A recovered file must have no pending intent: reopening again must be
    // a plain open with recovered_intent == None.
    let dir = tmp_dir("reopen");
    let file = dir.join("i.db");
    for k in 0u64..4 {
        run_crash(RunSpec {
            file: &file,
            op: "put",
            key: &k.to_string(),
            value: "v",
            crash: None,
            capacity: 4,
            hash: "u64lowbits",
            expect_ok: true,
        });
    }
    run_crash(RunSpec {
        file: &file,
        op: "put",
        key: "4",
        value: "v",
        crash: Some("split.after_commit_meta"),
        capacity: 4,
        hash: "u64lowbits",
        expect_ok: true,
    });
    {
        let idx = Index::open(&file).unwrap();
        assert_eq!(idx.recovered_intent(), Some("split"));
    }
    let mut idx = Index::open(&file).unwrap();
    assert!(idx.recovered_intent().is_none());
    let st = idx.stats().unwrap();
    assert_invariants(&st);
}

#[test]
fn collision_over_http_capacity_error_is_explicit() {
    // Direct engine-level equivalent of the HTTP 507 path.
    let dir = tmp_dir("httpcoll");
    let file = dir.join("i.db");
    let cfg = Config {
        bucket_capacity: 2,
        max_depth: 20,
        hash: HashKind::Constant,
        key_max: 128,
        val_max: 256,
    };
    let mut idx = Index::create(&file, &cfg).unwrap();
    idx.put(b"a", b"1").unwrap();
    idx.put(b"b", b"2").unwrap();
    let msg = idx.put(b"c", b"3").unwrap_err().to_string();
    assert!(
        msg.contains("collides"),
        "message should say collides: {}",
        msg
    );
    assert!(msg.contains("capacity exhausted"), "{}", msg);
}
