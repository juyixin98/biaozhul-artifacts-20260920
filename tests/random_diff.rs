//! Differential testing against `BTreeMap` over random keys:
//! point gets, full scans, bounded scans and validation agree, across many
//! seeds and block/interval sizes.

mod common;

use common::*;
use psst::table::{collect_scan, Bound, Options, ScanOptions};
use std::collections::BTreeMap;

#[test]
fn random_get_and_scan_match_btreemap_across_seeds() {
    for seed in 1u64..=40 {
        let mut rng = Rng::new(seed.wrapping_mul(0x0123_4567));
        let count = 1 + (rng.below(400) as usize);
        let map = random_map(&mut rng, count);
        let pairs = map_pairs(&map);

        let options = Options {
            block_size: 40 + (rng.below(300) as usize),
            restart_interval: 1 + (rng.below(8) as u32),
        };
        let (store, size) = build_mem(&pairs, options).expect("build");
        let table = open_mem(&store).expect("open");

        // Validation must pass for every generated file.
        let report = table.validate().expect("validation");
        assert_eq!(report.file_size, size);
        assert_eq!(report.total_entries, pairs.len());
        assert!(!report.data_blocks.is_empty());
        if pairs.len() > 6 {
            assert!(
                report.data_blocks.len() > 1,
                "seed {seed}: expected multiple small blocks"
            );
        }

        // 1. Every stored key returns exactly its value.
        for (k, v) in &map {
            assert_eq!(table.get(k).unwrap().as_ref(), Some(v));
        }

        // 2. Random probes: present keys match; absent keys return None.
        for _ in 0..300 {
            let mut probe = Vec::new();
            let len = 1 + (rng.below(24) as usize);
            rng.bytes(&mut probe, len);
            match table.get(&probe).unwrap() {
                Some(v) => assert_eq!(map.get(&probe).unwrap(), &v, "seed {seed}"),
                None => assert!(!map.contains_key(&probe), "seed {seed}"),
            }
        }

        // 3. Full scan equals the map in order.
        let all = collect_scan(&table, ScanOptions::default()).unwrap();
        assert_eq!(all, pairs, "seed {seed}: full scan mismatch");

        // 4. Bounded scans against BTreeMap ranges.
        for _ in 0..30 {
            let mut lo = Vec::new();
            let mut hi = Vec::new();
            let lo_len = 1 + rng.below(24) as usize;
            let hi_len = 1 + rng.below(24) as usize;
            rng.bytes(&mut lo, lo_len);
            rng.bytes(&mut hi, hi_len);
            if lo > hi {
                std::mem::swap(&mut lo, &mut hi);
            }
            let lo_excl = rng.next_u64() & 1 == 0;
            let hi_incl = rng.next_u64() & 1 == 0;

            let start = if lo_excl {
                Bound::Excluded(lo.clone())
            } else {
                Bound::Included(lo.clone())
            };
            let end = if hi_incl {
                Bound::Included(hi.clone())
            } else {
                Bound::Excluded(hi.clone())
            };

            let got = collect_scan(
                &table,
                ScanOptions {
                    start,
                    end,
                    limit: None,
                },
            )
            .unwrap();

            let expected: Vec<(Vec<u8>, Vec<u8>)> = map
                .range(lo.clone()..=hi.clone())
                .filter(|(k, _)| {
                    let ok_lo = if lo_excl { **k > lo } else { **k >= lo };
                    let ok_hi = if hi_incl { **k <= hi } else { **k < hi };
                    ok_lo && ok_hi
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            assert_eq!(got, expected, "seed {seed}: bounded scan mismatch");
        }

        // 5. Half-open / unbounded scans and limits.
        let first_key = pairs.first().unwrap().0.clone();
        let last_key = pairs.last().unwrap().0.clone();
        let from_third = collect_scan(
            &table,
            ScanOptions {
                start: Bound::Included(first_key.clone()),
                end: Bound::Excluded(last_key.clone()),
                limit: None,
            },
        )
        .unwrap();
        assert_eq!(from_third.len(), pairs.len() - 1);

        let limited = collect_scan(
            &table,
            ScanOptions {
                start: Bound::Unbounded,
                end: Bound::Unbounded,
                limit: Some(3),
            },
        )
        .unwrap();
        assert_eq!(limited.len(), pairs.len().min(3));
        assert_eq!(limited, pairs[..pairs.len().min(3)].to_vec());
    }
}

#[test]
fn random_large_table_multi_block() {
    // One dedicated, larger, strongly multi-block run.
    let mut rng = Rng::new(0xdead_beef);
    let map: BTreeMap<Vec<u8>, Vec<u8>> = (0..2000u32)
        .map(|i| {
            let k = format!("key/{i:08}").into_bytes();
            let mut v = Vec::new();
            rng.bytes(&mut v, 10 + (i as usize % 40));
            (k, v)
        })
        .collect();
    let pairs = map_pairs(&map);
    let (store, _) = build_mem(
        &pairs,
        Options {
            block_size: 256,
            restart_interval: 8,
        },
    )
    .unwrap();
    let table = open_mem(&store).unwrap();
    let report = table.validate().unwrap();
    assert!(
        report.data_blocks.len() > 50,
        "{}",
        report.data_blocks.len()
    );

    for (k, v) in &map {
        assert_eq!(table.get(k).unwrap().as_ref(), Some(v));
    }
    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(all.len(), pairs.len());
}
