//! Fault-injection tests: MockIO simulates I/O failures and torn writes;
//! the repository must surface errors and recover to the last valid state.

use tsblock::io::{FileIO, MockIO};
use tsblock::storage::{LateMode, Repo};

const BLOCK: usize = 8;

fn open(io: MockIO) -> Repo<MockIO> {
    Repo::open(io, LateMode::Reject, BLOCK).unwrap()
}

fn points(start: i64, n: i64) -> Vec<(i64, i64)> {
    (0..n).map(|i| (start + i, (start + i) * 10)).collect()
}

#[test]
fn sync_failure_surfaces_error() {
    let mut io = MockIO::new();
    io.fail_sync_on.push("data/".to_string());
    let mut repo = open(io);
    repo.append("s", &points(0, 5)).unwrap();
    let err = repo.flush("s").unwrap_err();
    assert!(err.to_string().contains("injected fault"));
}

#[test]
fn append_failure_surfaces_error() {
    let mut io = MockIO::new();
    // first append is the file header; allow it, fail the block append
    io.fail_appends_after = Some(1);
    let mut repo = open(io);
    repo.append("s", &points(0, 5)).unwrap();
    assert!(repo.flush("s").is_err());
}

#[test]
fn torn_tail_is_truncated_on_recovery() {
    // write two blocks, then simulate a crash mid-third-block by truncating
    // the file at half of the third block
    let mut repo = open(MockIO::new());
    repo.append("s", &points(0, 8)).unwrap();
    repo.flush("s").unwrap();
    repo.append("s", &points(8, 8)).unwrap();
    repo.flush("s").unwrap();
    repo.append("s", &points(16, 8)).unwrap();
    repo.flush("s").unwrap();

    let mut io = repo.into_io();
    let len = io.len("data/s.tsb").unwrap();
    // find start of third block: scan is done by recovery; emulate crash by
    // chopping 7 bytes off the end (inside the last block's payload)
    io.truncate("data/s.tsb", len - 7).unwrap();

    let repo = open(io);
    // the torn block is dropped; the first two blocks are intact
    assert_eq!(repo.query("s", 0, 1000).unwrap(), points(0, 16));
    let stats = repo.stats("s").unwrap();
    assert_eq!(stats.blocks, 2);
    assert_eq!(stats.points, 16);
}

#[test]
fn corrupt_block_crc_drops_only_that_block() {
    let mut repo = open(MockIO::new());
    repo.append("s", &points(0, 8)).unwrap();
    repo.flush("s").unwrap();
    repo.append("s", &points(8, 8)).unwrap();
    repo.flush("s").unwrap();

    let mut io = repo.into_io();
    // flip one byte inside the second block's payload
    let len = io.len("data/s.tsb").unwrap();
    let mut data = io.read_all("data/s.tsb").unwrap();
    let pos = (len - 3) as usize;
    data[pos] ^= 0x5A;
    io.files.insert("data/s.tsb".to_string(), data);

    let repo = open(io);
    assert_eq!(repo.query("s", 0, 1000).unwrap(), points(0, 8));
    assert_eq!(repo.stats("s").unwrap().blocks, 1);
}

#[test]
fn read_failure_surfaces_on_open_and_query() {
    let mut repo = open(MockIO::new());
    repo.append("s", &points(0, 8)).unwrap();
    repo.flush("s").unwrap();
    let mut io = repo.into_io();
    io.fail_reads_on.push("data/".to_string());
    // recovery reads the data file => open fails
    assert!(Repo::open(io, LateMode::Reject, BLOCK).is_err());

    // query-time read failure: reads fail only after a successful open
    let mut repo = open(MockIO::new());
    repo.append("s", &points(0, 8)).unwrap();
    repo.flush("s").unwrap();
    repo.io_mut().fail_reads_on.push("data/".to_string());
    assert!(repo.query("s", 0, 1000).is_err());
}

#[test]
fn corrupt_late_log_tail_is_truncated() {
    let mut repo = Repo::open(MockIO::new(), LateMode::Buffer, BLOCK).unwrap();
    repo.append("s", &[(100, 1), (50, 0)]).unwrap(); // (50,0) -> late log
    repo.flush("s").unwrap();
    let mut io = repo.into_io();
    // append 5 stray bytes to the late log (torn record)
    io.append("late/s.tsl", &[1, 2, 3, 4, 5]).unwrap();
    let repo = Repo::open(io, LateMode::Buffer, BLOCK).unwrap();
    assert_eq!(
        repo.query("s", 0, 1000).unwrap(),
        vec![(50, 0), (100, 1)]
    );
    assert_eq!(repo.stats("s").unwrap().late_points, 1);
}

#[test]
fn recovery_allows_continued_writes() {
    let mut repo = open(MockIO::new());
    repo.append("s", &points(0, 8)).unwrap();
    repo.flush("s").unwrap();
    let mut io = repo.into_io();
    let len = io.len("data/s.tsb").unwrap();
    io.append("data/s.tsb", &[0xDE, 0xAD, 0xBE]).unwrap(); // torn tail
    let _ = len;
    let mut repo = open(io);
    // torn tail truncated; new writes continue cleanly
    repo.append("s", &points(8, 8)).unwrap();
    repo.flush("s").unwrap();
    assert_eq!(repo.query("s", 0, 1000).unwrap(), points(0, 16));
}
