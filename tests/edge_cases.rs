//! Acceptance edge cases: empty keys, long common prefixes and cross-block
//! range scans, called out explicitly in the project requirements.

mod common;

use common::*;
use psst::table::{collect_scan, Bound, Options, ScanOptions};

#[test]
fn empty_key_point_get_and_ordering() {
    let pairs = vec![
        (vec![], b"empty".to_vec()),
        (vec![0x00], b"zero".to_vec()),
        (vec![0x00, 0x00], b"zeros".to_vec()),
        (b"a".to_vec(), b"a".to_vec()),
    ];
    let (store, _) = build_mem(&pairs, tiny_options()).unwrap();
    let table = open_mem(&store).unwrap();
    table.validate().unwrap();

    assert_eq!(table.get(&[]).unwrap(), Some(b"empty".to_vec()));
    assert_eq!(table.get(&[0x00]).unwrap(), Some(b"zero".to_vec()));
    assert_eq!(table.get(&[0x00, 0x00]).unwrap(), Some(b"zeros".to_vec()));
    assert_eq!(table.get(b"missing").unwrap(), None);

    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(all, pairs);

    // Scan starting exactly at the empty key returns everything.
    let from_empty = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(vec![]),
            end: Bound::Unbounded,
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(from_empty, pairs);

    // Start boundary strictly greater than empty skips it.
    let after_empty = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Excluded(vec![]),
            end: Bound::Unbounded,
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(after_empty, pairs[1..].to_vec());
}

#[test]
fn long_common_prefix_still_gets_and_scans() {
    let prefix = b"prefix-compression-shared-prefix-".repeat(20); // ~680 bytes
    let mut pairs = Vec::new();
    for i in 0..60u32 {
        let mut k = prefix.clone();
        k.extend_from_slice(format!("{i:06}").as_bytes());
        let mut v = vec![i as u8; 1 + (i as usize % 5)];
        v.extend_from_slice(b"value");
        pairs.push((k, v));
    }
    // Also a key that is a strict prefix of others.
    pairs.insert(0, (b"p".to_vec(), b"single-p".to_vec()));
    pairs.sort_by(|a, b| a.0.cmp(&b.0));
    pairs.dedup_by(|a, b| a.0 == b.0);

    let (store, raw_size) = build_mem(
        &pairs,
        Options {
            // The target must exceed one full ~700-byte restart entry,
            // otherwise every block holds a single entry with a full key.
            // At 8 KiB each block holds several entries that share the
            // 680-byte prefix — that is exactly what prefix compression buys.
            block_size: 8 * 1024,
            restart_interval: 16,
        },
    )
    .unwrap();
    let table = open_mem(&store).unwrap();
    let report = table.validate().unwrap();
    assert!(!report.data_blocks.is_empty());

    // Prefix compression must actually shrink the file: naive storage would be
    // at least sum(key.len()) bytes plus overhead.
    let naive_keys: usize = pairs.iter().map(|(k, _)| k.len()).sum();
    assert!(
        (raw_size as usize) < naive_keys,
        "file {raw_size} should be smaller than naive key bytes {naive_keys}"
    );

    for (k, v) in &pairs {
        assert_eq!(table.get(k).unwrap().as_ref(), Some(v));
    }
    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(all, pairs);
}

#[test]
fn cross_block_scan_with_narrow_bounds() {
    // Keys distributed over many blocks; ranges deliberately start and end
    // inside different blocks and exercise every alignment (block boundary,
    // restart-point boundary, mid-interval entry).
    let pairs: Vec<(Vec<u8>, Vec<u8>)> = (0..500u32)
        .map(|i| {
            (
                format!("k/{i:05}").into_bytes(),
                format!("v{i}").into_bytes(),
            )
        })
        .collect();
    let (store, _) = build_mem(
        &pairs,
        Options {
            block_size: 110,
            restart_interval: 4,
        },
    )
    .unwrap();
    let table = open_mem(&store).unwrap();
    let report = table.validate().unwrap();
    assert!(report.data_blocks.len() > 20);

    // Range that spans most of the file.
    let mid = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k/00100".to_vec()),
            end: Bound::Excluded(b"k/00400".to_vec()),
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(mid.len(), 300);
    assert_eq!(mid.first().unwrap().0, b"k/00100");
    assert_eq!(mid.last().unwrap().0, b"k/00399");

    // Inclusive end on a key that sits at a block/ restart boundary.
    let incl = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k/00000".to_vec()),
            end: Bound::Included(b"k/00004".to_vec()),
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(incl.len(), 5);

    // Start strictly between existing keys.
    let gap = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k/00123X".to_vec()),
            end: Bound::Excluded(b"k/00126".to_vec()),
            limit: None,
        },
    )
    .unwrap();
    assert_eq!(
        gap.iter().map(|(k, _)| k.clone()).collect::<Vec<_>>(),
        vec![b"k/00124".to_vec(), b"k/00125".to_vec()]
    );

    // Empty range: start past the end.
    let none = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"z".to_vec()),
            end: Bound::Unbounded,
            limit: None,
        },
    )
    .unwrap();
    assert!(none.is_empty());

    // Empty range contained within one block gap.
    let none2 = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k/00200X".to_vec()),
            end: Bound::Excluded(b"k/00200Y".to_vec()),
            limit: None,
        },
    )
    .unwrap();
    assert!(none2.is_empty());

    // Limit applied mid cross-block scan.
    let lim = collect_scan(
        &table,
        ScanOptions {
            start: Bound::Included(b"k/00000".to_vec()),
            end: Bound::Unbounded,
            limit: Some(37),
        },
    )
    .unwrap();
    assert_eq!(lim.len(), 37);
}

#[test]
fn empty_table_is_usable_and_valid() {
    let (store, _) = build_mem(&[], Options::default()).unwrap();
    let table = open_mem(&store).unwrap();
    let report = table.validate().unwrap();
    assert_eq!(report.total_entries, 0);
    assert!(report.data_blocks.is_empty());
    assert_eq!(table.get(b"anything").unwrap(), None);
    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert!(all.is_empty());
}
