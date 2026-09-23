//! Acceptance tests: every query result is verified against a reference
//! uncompressed array (Vec<(i64, i64)>). Covers boundary queries, negative
//! values, duplicate timestamps, i64 extremes, reopen persistence, and
//! reports the real compression ratio.
//!
//! Run with output:  cargo test --test acceptance -- --nocapture

use std::path::PathBuf;
use tsblock::io::FsIO;
use tsblock::storage::{LateMode, Repo, DEFAULT_BLOCK_SIZE};

const BLOCK: usize = 64; // small blocks to exercise block boundaries

fn test_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("tsblock-acc-{}-{}", std::process::id(), name));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn open(dir: &PathBuf, mode: LateMode) -> Repo<FsIO> {
    Repo::open(FsIO::new(dir), mode, BLOCK).unwrap()
}

/// Reference filter: inclusive range over the uncompressed array.
fn reference_query(data: &[(i64, i64)], from: i64, to: i64) -> Vec<(i64, i64)> {
    data.iter()
        .copied()
        .filter(|p| p.0 >= from && p.0 <= to)
        .collect()
}

fn regular_dataset(n: i64) -> Vec<(i64, i64)> {
    // 1-second cadence, values crossing zero (negatives included)
    (0..n)
        .map(|i| (1_700_000_000 + i, ((i * 37) % 1001) - 500))
        .collect()
}

#[test]
fn full_and_boundary_queries_match_reference() {
    let dir = test_dir("boundary");
    let data = regular_dataset(1000);

    let mut repo = open(&dir, LateMode::Reject);
    let outcome = repo.append("cpu", &data).unwrap();
    assert_eq!(outcome.accepted, data.len());
    repo.flush("cpu").unwrap();

    // block boundaries: with BLOCK=64, blocks start at ts offsets 0,64,128,...
    let base = data[0].0;
    let cases: Vec<(i64, i64)> = vec![
        (base, base + 999),                 // full range
        (base, base),                       // single first point
        (base + 999, base + 999),           // single last point
        (base + 64, base + 127),            // exactly one block
        (base + 63, base + 64),             // straddling a boundary
        (base + 64, base + 64),             // first point of a block
        (base + 127, base + 128),           // last of block + first of next
        (base - 1000, base - 1),            // entirely before
        (base + 1000, base + 2000),         // entirely after
        (base + 500, base + 499),           // (invalid, tested separately)
    ];
    for &(from, to) in &cases[..9] {
        let got = repo.query("cpu", from, to).unwrap();
        let want = reference_query(&data, from, to);
        assert_eq!(got, want, "range [{}, {}]", from, to);
    }
    // invalid range must error
    assert!(repo.query("cpu", base + 500, base + 499).is_err());
    // unknown series => empty
    assert_eq!(repo.query("nope", 0, i64::MAX).unwrap(), vec![]);
}

#[test]
fn negative_values_and_zero_crossing() {
    let dir = test_dir("negatives");
    let data: Vec<(i64, i64)> = vec![
        (10, -500),
        (20, -1),
        (30, 0),
        (40, 1),
        (50, -9_000_000_000),
        (60, 9_000_000_000),
        (70, -9_000_000_001), // big negative swing => large deltas
    ];
    let mut repo = open(&dir, LateMode::Reject);
    repo.append("v", &data).unwrap();
    repo.flush("v").unwrap();
    assert_eq!(repo.query("v", i64::MIN, i64::MAX).unwrap(), data);
    assert_eq!(repo.query("v", 20, 50).unwrap(), reference_query(&data, 20, 50));
}

#[test]
fn duplicate_and_out_of_order_rejected() {
    let dir = test_dir("reject");
    let mut repo = open(&dir, LateMode::Reject);
    let outcome = repo
        .append(
            "s",
            &[
                (100, 1),
                (200, 2),
                (200, 22), // duplicate
                (150, 3),  // out of order
                (300, 4),
                (100, 5),  // out of order (earlier ts)
            ],
        )
        .unwrap();
    assert_eq!(outcome.accepted, 3);
    assert_eq!(outcome.rejected.len(), 3);
    assert_eq!(outcome.rejected[0].reason, "duplicate");
    assert_eq!(outcome.rejected[1].reason, "out_of_order");
    assert_eq!(outcome.rejected[2].reason, "out_of_order");
    repo.flush("s").unwrap();
    assert_eq!(
        repo.query("s", 0, 1000).unwrap(),
        vec![(100, 1), (200, 2), (300, 4)]
    );
}

#[test]
fn late_buffer_mode_merges_at_query() {
    let dir = test_dir("buffer");
    let mut repo = open(&dir, LateMode::Buffer);
    let outcome = repo
        .append(
            "s",
            &[
                (100, 1),
                (300, 3),
                (200, 2),   // late -> buffered
                (300, 33),  // duplicate of main-stream ts -> buffered, loses on merge
                (50, 0),    // late -> buffered
                (200, 22),  // duplicate within late log -> first-write-wins
            ],
        )
        .unwrap();
    assert_eq!(outcome.accepted, 2);
    assert_eq!(outcome.buffered, 4);
    repo.flush("s").unwrap();
    // merged, sorted; main stream wins on collision; late-log first-write-wins
    assert_eq!(
        repo.query("s", 0, 1000).unwrap(),
        vec![(50, 0), (100, 1), (200, 2), (300, 3)]
    );
    // range queries only see matching late points
    assert_eq!(repo.query("s", 0, 99).unwrap(), vec![(50, 0)]);
}

#[test]
fn i64_extremes_roundtrip() {
    let dir = test_dir("extremes");
    // timestamps near i64::MAX, values at i64::MIN/MAX; deltas overflow i64
    let data: Vec<(i64, i64)> = vec![
        (0, i64::MIN),
        (1, i64::MAX),
        (2, i64::MIN),
        (i64::MAX - 2, 0),
        (i64::MAX - 1, i64::MAX),
        (i64::MAX, i64::MIN),
    ];
    let mut repo = open(&dir, LateMode::Reject);
    let outcome = repo.append("x", &data).unwrap();
    assert_eq!(outcome.accepted, data.len());
    repo.flush("x").unwrap();
    assert_eq!(repo.query("x", i64::MIN, i64::MAX).unwrap(), data);
    assert_eq!(
        repo.query("x", i64::MAX - 1, i64::MAX).unwrap(),
        vec![(i64::MAX - 1, i64::MAX), (i64::MAX, i64::MIN)]
    );
}

#[test]
fn reopen_preserves_flushed_data() {
    let dir = test_dir("reopen");
    let data = regular_dataset(500);
    {
        let mut repo = open(&dir, LateMode::Reject);
        repo.append("cpu", &data[..300]).unwrap();
        repo.flush("cpu").unwrap();
        repo.append("cpu", &data[300..]).unwrap(); // left pending, not durable
    }
    {
        let repo = open(&dir, LateMode::Reject);
        // only the flushed prefix survived
        assert_eq!(repo.query("cpu", 0, i64::MAX).unwrap(), data[..300].to_vec());
    }
    // append the rest after reopen, flush, verify everything
    {
        let mut repo = open(&dir, LateMode::Reject);
        repo.append("cpu", &data[300..]).unwrap();
        repo.flush("cpu").unwrap();
        assert_eq!(repo.query("cpu", 0, i64::MAX).unwrap(), data);
    }
}

#[test]
fn compression_ratio_report() {
    let dir = test_dir("ratio");
    let n = 10_000i64;
    let data = regular_dataset(n);
    let mut repo = open(&dir, LateMode::Reject);
    repo.append("cpu", &data).unwrap();
    repo.flush("cpu").unwrap();

    let stats = repo.stats("cpu").unwrap();
    let ratio = stats.raw_bytes as f64 / stats.file_bytes as f64;
    println!(
        "regular 1s-cadence data: {} points, raw {} bytes -> file {} bytes, ratio {:.2}x ({} blocks)",
        stats.points, stats.raw_bytes, stats.file_bytes, ratio, stats.blocks
    );
    assert_eq!(stats.points as i64, n);
    assert_eq!(stats.raw_bytes, 16 * n as u64);
    // regular data must actually compress
    assert!(stats.file_bytes < stats.raw_bytes);

    // random-ish data compresses less; report honestly
    let mut rng: u64 = 0x9E3779B97F4A7C15;
    let mut noisy = Vec::new();
    let mut ts = 0i64;
    for _ in 0..n {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        ts += 1 + (rng % 3) as i64; // jittered cadence
        noisy.push((ts, (rng as i64) >> 16));
    }
    repo.append("noisy", &noisy).unwrap();
    repo.flush("noisy").unwrap();
    let s2 = repo.stats("noisy").unwrap();
    println!(
        "jittered random data:     {} points, raw {} bytes -> file {} bytes, ratio {:.2}x",
        s2.points,
        s2.raw_bytes,
        s2.file_bytes,
        s2.raw_bytes as f64 / s2.file_bytes as f64
    );

    // sanity: full read-back of both series matches reference
    assert_eq!(repo.query("cpu", 0, i64::MAX).unwrap(), data);
    assert_eq!(repo.query("noisy", 0, i64::MAX).unwrap(), noisy);
}

#[test]
fn unflushed_points_visible_but_not_durable() {
    let dir = test_dir("pending");
    let mut repo = open(&dir, LateMode::Reject);
    repo.append("s", &[(1, 10), (2, 20)]).unwrap();
    // visible to queries in the same process before flush
    assert_eq!(repo.query("s", 0, 10).unwrap(), vec![(1, 10), (2, 20)]);
    let stats = repo.stats("s").unwrap();
    assert_eq!(stats.pending, 2);
    assert_eq!(stats.points, 0);
    drop(repo);
    // after reopen without flush: gone (documented sync boundary)
    let repo = open(&dir, LateMode::Reject);
    assert_eq!(repo.query("s", 0, 10).unwrap(), vec![]);
}

#[test]
fn default_block_size_constant_is_sane() {
    assert!(DEFAULT_BLOCK_SIZE >= 64);
}
