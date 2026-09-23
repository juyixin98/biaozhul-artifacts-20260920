//! File-backed atomic build path: temp-file + rename, duplicate-name conflict,
//! temp cleanup on failure, and reopen/validate of a real on-disk table.

use psst::error::Error;
use psst::table::{build_table_sorted, collect_scan, Bound, Options, ScanOptions, Table};

fn unique_dir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "psst-file-test-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

#[test]
fn atomic_build_rename_and_reopen() {
    let dir = unique_dir("atomic");
    let entries = vec![
        (vec![1u8, 2, 3], b"v1".to_vec()),
        (vec![1, 2, 4], b"v2".to_vec()),
        (vec![9], b"v3".to_vec()),
    ];
    let stats = build_table_sorted(&dir, "t1", &entries, Options::default(), 1).unwrap();
    assert_eq!(stats.entries, 3);
    assert_eq!(stats.data_blocks, 1);

    // No leftover temp files.
    let leftovers: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with('.'))
        .collect();
    assert!(
        leftovers.is_empty(),
        "temp files left behind: {leftovers:?}"
    );

    // Reopen from the real path and read.
    let table = Table::open_path(&dir.join("t1")).unwrap();
    let report = table.validate().unwrap();
    assert_eq!(report.total_entries, 3);
    assert_eq!(table.get(&[9]).unwrap(), Some(b"v3".to_vec()));
    assert_eq!(table.get(&[0]).unwrap(), None);

    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn build_sorts_unsorted_input_and_rejects_duplicates() {
    let dir = unique_dir("sort");
    let entries = vec![
        (b"c".to_vec(), b"3".to_vec()),
        (b"a".to_vec(), b"1".to_vec()),
        (b"b".to_vec(), b"2".to_vec()),
    ];
    build_table_sorted(&dir, "s", &entries, Options::default(), 7).unwrap();
    let table = Table::open_path(&dir.join("s")).unwrap();
    let got = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(
        got,
        vec![
            (b"a".to_vec(), b"1".to_vec()),
            (b"b".to_vec(), b"2".to_vec()),
            (b"c".to_vec(), b"3".to_vec()),
        ]
    );

    let dup = vec![
        (b"k".to_vec(), b"1".to_vec()),
        (b"k".to_vec(), b"2".to_vec()),
    ];
    let err = build_table_sorted(&dir, "d", &dup, Options::default(), 8).unwrap_err();
    assert!(matches!(err, Error::InvalidArgument(_)));

    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn duplicate_table_name_is_conflict_and_temp_is_cleaned() {
    let dir = unique_dir("dup");
    let entries = vec![(b"k".to_vec(), b"v".to_vec())];
    build_table_sorted(&dir, "once", &entries, Options::default(), 1).unwrap();
    let err = build_table_sorted(&dir, "once", &entries, Options::default(), 2).unwrap_err();
    assert!(
        matches!(err, Error::Io(ref e) if e.kind() == std::io::ErrorKind::AlreadyExists),
        "got {err}"
    );

    // Existing file untouched and no temp left behind.
    let table = Table::open_path(&dir.join("once")).unwrap();
    assert_eq!(table.get(b"k").unwrap(), Some(b"v".to_vec()));
    let any_temp = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .any(|e| e.file_name().to_string_lossy().contains(".tmp."));
    assert!(!any_temp);

    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn invalid_table_names_rejected() {
    let dir = unique_dir("names");
    let entries: Vec<(Vec<u8>, Vec<u8>)> = vec![];
    for bad in ["", ".", "..", "a/b", "a\\b", "a\0b"] {
        assert!(
            build_table_sorted(&dir, bad, &entries, Options::default(), 3).is_err(),
            "name {bad:?} should be rejected"
        );
    }
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn scan_from_real_file_with_bounds() {
    let dir = unique_dir("scan");
    let entries: Vec<(Vec<u8>, Vec<u8>)> = (0..100u32)
        .map(|i| {
            (
                format!("k{i:03}").into_bytes(),
                format!("v{i}").into_bytes(),
            )
        })
        .collect();
    build_table_sorted(
        &dir,
        "r",
        &entries,
        Options {
            block_size: 128,
            restart_interval: 4,
        },
        5,
    )
    .unwrap();
    let table = Table::open_path(&dir.join("r")).unwrap();
    table.validate().unwrap();

    let got = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k010".to_vec()),
            end: Bound::Included(b"k012".to_vec()),
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(got.len(), 3);
    assert_eq!(got.first().unwrap().0, b"k010");
    assert_eq!(got.last().unwrap().0, b"k012");

    std::fs::remove_dir_all(&dir).unwrap();
}
