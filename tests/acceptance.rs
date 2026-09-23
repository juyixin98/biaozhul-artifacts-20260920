//! End-to-end acceptance tests.
//!
//! These drive the real repository + engine through the public API with
//! deliberately tiny memory budgets, compare against the in-memory reference
//! sort, and exercise empty input, an over-budget single line, segment
//! corruption, and crash/fault recovery from completed segments.

use std::io::Write;

use extsort::io::{FaultKind, FaultPlan};
use extsort::testutil::{reference, Harness};

// The accounting floor for two merge lanes + buffers. Use a comfortably larger
// but still tiny budget so many runs are produced.
const TINY: u64 = 160 * 1024;
const LANES: usize = 2;

fn dataset(rows: usize, seed: u64) -> Vec<u8> {
    // Small deterministic LCG so no rand dependency is needed.
    let mut s = seed
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407);
    let mut next = |n: u64| {
        s ^= s >> 12;
        s = s.wrapping_mul(2862933555777941757);
        s ^= s >> 13;
        s % n
    };
    let mut out = Vec::new();
    for i in 0..rows {
        let g = next(24);
        let nlen = 2 + next(9) as usize;
        let mut name = Vec::with_capacity(nlen);
        for _ in 0..nlen {
            name.push(b'a' + next(7) as u8);
        }
        let _ = write!(out, "{g:02},{},{i}\n", std::str::from_utf8(&name).unwrap());
    }
    out
}

#[test]
fn empty_input_produces_empty_output() {
    let mut h = Harness::new("empty", "", TINY, LANES, false);
    h.write_input(b"");
    let st = h.run().expect("run empty");
    assert_eq!(st.records, 0);
    assert_eq!(h.output(), b"");
    h.cleanup();
}

#[test]
fn single_line_no_newline_normalized() {
    let mut h = Harness::new("one", "1", TINY, LANES, false);
    h.write_input(b"alpha,beta");
    h.run().unwrap();
    assert_eq!(h.output(), b"alpha,beta\n");
    h.cleanup();
}

#[test]
fn multi_pass_merge_matches_reference_asc() {
    let data = dataset(20_000, 11);
    let spec = "1:asc,2:asc";
    let mut h = Harness::new("mp-asc", spec, TINY, LANES, true);
    h.write_input(&data);
    let st = h.run().expect("run");
    // 20k rows in a tiny budget must force several level-0 runs and >0 passes.
    assert!(
        st.level0_runs >= 5,
        "expected many runs, got {}",
        st.level0_runs
    );
    assert!(
        st.merge_passes >= 2,
        "expected multi-pass, got {}",
        st.merge_passes
    );
    assert_eq!(h.output(), reference(&data, spec));
    h.cleanup();
}

#[test]
fn composite_asc_desc_with_stability_matches_reference() {
    // Deliberately include repeated (col1,col2) pairs so the seq tie-break is
    // observable via col3.
    let pattern = b"1,aa,0\n1,bb,1\n2,zz,2\n1,aa,3\n1,aa,4\n2,zz,5\n1,bb,6\n";
    let repeats = 1500; // ~86 KB -> several level-0 runs at the tiny budget
    let mut data = Vec::with_capacity(pattern.len() * repeats);
    for _ in 0..repeats {
        data.extend_from_slice(pattern);
    }
    let spec = "1:asc,2:desc";
    let mut h = Harness::new("mp-mix", spec, TINY, LANES, true);
    h.write_input(&data);
    let st = h.run().unwrap();
    assert!(
        st.level0_runs > 1,
        "expected multiple runs, got {}",
        st.level0_runs
    );
    assert!(st.merge_passes >= 1);
    assert_eq!(h.output(), reference(&data, spec));
    h.cleanup();
}

#[test]
fn whole_line_default_key_matches_reference() {
    let data = dataset(5_000, 23);
    let mut h = Harness::new("mp-line", "", TINY, LANES, true);
    h.write_input(&data);
    h.run().unwrap();
    assert_eq!(h.output(), reference(&data, ""));
    h.cleanup();
}

#[test]
fn giant_single_line_larger_than_budget_preserves_bytes() {
    // 300 KB value in column 2; key is column 1, so it streams past the budget.
    let mut data = b"k1,".to_vec();
    data.extend(std::iter::repeat(b'x').take(300_000));
    data.extend_from_slice(b"\nzz,second\n");

    let spec = "1:asc";
    let mut h = Harness::new("giant", spec, TINY, LANES, true);
    h.write_input(&data);
    let st = h.run().expect("run giant");
    assert_eq!(st.records, 2);
    let out = h.output();
    let mut lines = out.splitn(3, |&b| b == b'\n');
    let first = lines.next().unwrap();
    let second = lines.next().unwrap();
    let mut expected = b"k1,".to_vec();
    expected.extend(std::iter::repeat(b'x').take(300_000));
    assert_eq!(first, expected.as_slice());
    assert_eq!(second, b"zz,second");
    h.cleanup();
}

#[test]
fn many_records_then_one_giant_interleaved() {
    let mut data = dataset(3_000, 47);
    let mut giant = b"gk,".to_vec();
    giant.extend(std::iter::repeat(b'Q').take(90_000));
    giant.push(b'\n');
    data.extend_from_slice(&giant);
    data.extend_from_slice(b"aa,last\n");

    let spec = "1:asc";
    let mut h = Harness::new("giantmix", spec, TINY, LANES, true);
    h.write_input(&data);
    h.run().unwrap();
    assert_eq!(h.output(), reference(&data, spec));
    h.cleanup();
}

#[test]
fn whole_line_key_giant_is_rejected_not_silently_oversized() {
    // With the default whole-line key, a line larger than the head window must
    // fail loudly rather than exceed the memory budget.
    let mut data = Vec::new();
    data.extend(std::iter::repeat(b'z').take(50_000));
    data.push(b'\n');
    let mut h = Harness::new("toolarge", "", TINY, LANES, true);
    h.write_input(&data);
    let err = h.run().expect_err("should reject an unresolvable key");
    assert!(matches!(err, extsort::Error::KeyTooLarge { .. }), "{err:?}");
    h.cleanup();
}

#[test]
fn injected_write_fault_then_resume_completes_and_matches() {
    let data = dataset(20_000, 99);
    let spec = "1:asc,2:asc";

    // First attempt: fail creation of a later level-0 run. Earlier runs remain
    // durably committed and are the recovery point.
    let plan = FaultPlan::new().add(FaultKind::CreateFail, "run-00008", 1);
    let mut h = Harness::with_faults("rec-map", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    let err = h.run().expect_err("injected fault must surface");
    assert!(matches!(err, extsort::Error::Fault(_)), "{err:?}");
    // Do NOT cleanup: reopen the on-disk job through recovery.

    let (repo, mut job) = Harness::reopen("rec-map", &root);
    let st = extsort::run_job(&repo, &mut job).expect("resume completes");
    assert!(st.resumed);
    let out = extsort::io::read_all(repo.vfs().as_ref(), "jobs/rec-map/output.txt").unwrap();
    assert_eq!(out, reference(&data, spec));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn injected_merge_fault_then_resume_completes() {
    let data = dataset(20_000, 5);
    let spec = "1:asc";
    // Fail the first level-1 merge output; map runs stay committed.
    let plan = FaultPlan::new().add(FaultKind::CreateFail, "m-1-", 1);
    let mut h = Harness::with_faults("rec-merge", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    assert!(matches!(
        h.run().expect_err("fault"),
        extsort::Error::Fault(_)
    ));

    let (repo, mut job) = Harness::reopen("rec-merge", &root);
    let st = extsort::run_job(&repo, &mut job).expect("resume");
    assert!(st.resumed);
    let out = extsort::io::read_all(repo.vfs().as_ref(), "jobs/rec-merge/output.txt").unwrap();
    assert_eq!(out, reference(&data, spec));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn injected_merge_fault_on_a_later_batch_then_resume_completes() {
    // Regression: a fault after several merge batches committed used to lose the
    // already-merged records (inputs deleted per batch / manifest drained before
    // the failing commit). Inputs must survive until the whole pass rotates.
    let data = dataset(20_000, 101);
    let spec = "1:asc,2:asc";
    // 16-ish level0 runs / 2 lanes -> ~8 level1 batches; fail the 4th.
    let plan = FaultPlan::new().add(FaultKind::CreateFail, "m-1-00003", 1);
    let mut h = Harness::with_faults("rec-late-merge", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    assert!(matches!(
        h.run().expect_err("fault"),
        extsort::Error::Fault(_)
    ));

    let (repo, mut job) = Harness::reopen("rec-late-merge", &root);
    extsort::run_job(&repo, &mut job).expect("resume");
    let out = extsort::io::read_all(repo.vfs().as_ref(), "jobs/rec-late-merge/output.txt").unwrap();
    assert_eq!(out, reference(&data, spec));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn corrupted_segment_payload_is_detected_on_reopen() {
    let data = dataset(20_000, 71);
    let spec = "1:asc,2:asc";
    // Stop map phase after several committed runs.
    let plan = FaultPlan::new().add(FaultKind::CreateFail, "run-00006", 1);
    let mut h = Harness::with_faults("corrupt-payload", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    let _ = h.run().expect_err("fault");

    // Bit-flip a byte inside a committed run's payload region.
    let run = root.join("jobs/corrupt-payload/level0/run-00002.run");
    let mut bytes = std::fs::read(&run).unwrap();
    bytes[120] ^= 0xFF;
    std::fs::write(&run, bytes).unwrap();

    let repo = extsort::Repository::new(&root);
    let err = match repo.open_job("corrupt-payload") {
        Ok(_) => panic!("expected corruption to be detected"),
        Err(e) => e,
    };
    assert!(matches!(err, extsort::Error::Corrupt { .. }), "{err:?}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn truncated_segment_is_detected_on_reopen() {
    let data = dataset(20_000, 73);
    let spec = "1:asc,2:asc";
    let plan = FaultPlan::new().add(FaultKind::CreateFail, "run-00006", 1);
    let mut h = Harness::with_faults("corrupt-trunc", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    let _ = h.run().expect_err("fault");

    let run = root.join("jobs/corrupt-trunc/level0/run-00001.run");
    let len = std::fs::metadata(&run).unwrap().len();
    let f = std::fs::OpenOptions::new().write(true).open(&run).unwrap();
    f.set_len(len - 24).unwrap();
    drop(f);

    let repo = extsort::Repository::new(&root);
    let err = match repo.open_job("corrupt-trunc") {
        Ok(_) => panic!("expected truncation to be detected"),
        Err(e) => e,
    };
    assert!(matches!(err, extsort::Error::Corrupt { .. }), "{err:?}");
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn orphan_tmp_files_are_discarded_on_recovery() {
    let data = dataset(8_000, 13);
    let spec = "1:asc";
    // Abort during a run write, leaving a *.run.tmp with no rename.
    let plan = FaultPlan::new().add(FaultKind::SyncFail, "run-00003.run.tmp", 1);
    let mut h = Harness::with_faults("orphan-tmp", spec, TINY, LANES, plan);
    let root = h.root.clone();
    h.write_input(&data);
    let _ = h.run().expect_err("sync fault");

    // The orphan tmp exists before recovery.
    let lvl = root.join("jobs/orphan-tmp/level0");
    assert!(std::fs::read_dir(&lvl).unwrap().any(|e| e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .ends_with(".tmp")));

    let (repo, mut job) = Harness::reopen("orphan-tmp", &root);
    extsort::run_job(&repo, &mut job).expect("resume ignores orphan tmp");
    // Orphan removed by cleanup_temps.
    assert!(std::fs::read_dir(&lvl).unwrap().all(|e| !e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .ends_with(".tmp")));
    let out = extsort::io::read_all(repo.vfs().as_ref(), "jobs/orphan-tmp/output.txt").unwrap();
    assert_eq!(out, reference(&data, spec));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn budget_under_floor_is_rejected() {
    let mut h = Harness::new("lowbudget", "", 1024, LANES, false);
    h.write_input(b"a\nb\n");
    let err = h.run().expect_err("budget too small");
    assert!(
        matches!(err, extsort::Error::BudgetTooSmall { .. }),
        "{err:?}"
    );
    h.cleanup();
}
