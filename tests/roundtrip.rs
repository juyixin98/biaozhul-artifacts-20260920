//! Round-trip tests: boundary bit widths 0 and 64, negative values,
//! extreme outliers, multi-block streams and single-block access.

use bitpack::limits::Limits;
use bitpack::{decode_block_at, decode_stream, encode_stream, inspect};

fn limits() -> Limits {
    Limits::default()
}

fn roundtrip(values: &[i64], block_size: u32) -> Vec<u8> {
    let data = encode_stream(values, block_size).unwrap();
    let decoded = decode_stream(&data, &limits()).unwrap();
    assert_eq!(decoded, values, "roundtrip failed (block_size {block_size})");
    data
}

/// Simple deterministic PRNG (xorshift64) so tests need no external crates.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
}

#[test]
fn empty_and_single_value() {
    roundtrip(&[], 128);
    roundtrip(&[0], 128);
    roundtrip(&[i64::MIN], 128);
    roundtrip(&[i64::MAX], 128);
}

#[test]
fn constant_values_use_bit_width_zero() {
    let data = roundtrip(&[-7; 500], 128);
    let info = inspect(&data, &limits()).unwrap();
    // 500 values / 128 per block = 4 blocks; width-0 blocks are tiny.
    assert_eq!(info.header.block_count, 4);
    assert!(data.len() < 4 * 40 + 32 + 4 * 16);
}

#[test]
fn negative_and_mixed_signs() {
    let values: Vec<i64> = (0..300).map(|i| (i % 17) as i64 - 8).collect();
    roundtrip(&values, 32);
}

#[test]
fn forced_bit_widths_0_through_64() {
    // For each width w, build a block whose deltas need exactly w bits.
    for w in 0..=64u32 {
        let delta: i64 = match w {
            0 => 0,
            1 => -1,
            _ => 1i64 << (w - 2),
        };
        let values: Vec<i64> = (0..32).map(|i| if i == 0 { 0 } else { delta }).collect();
        let data = roundtrip(&values, 64);
        let info = inspect(&data, &limits()).unwrap();
        assert_eq!(info.header.block_count, 1);
        let block = decode_block_at(&data, 0, &limits()).unwrap();
        assert_eq!(block, values, "width {w}");
    }
}

#[test]
fn extreme_outliers_escape_and_roundtrip() {
    let mut values = vec![1000i64; 256];
    for (i, outlier) in [
        (3usize, i64::MIN),
        (77, i64::MAX),
        (128, i64::MIN + 1),
        (200, i64::MAX - 1),
        (255, -i64::MAX),
    ] {
        values[i] = outlier;
    }
    let data = roundtrip(&values, 64);
    let info = inspect(&data, &limits()).unwrap();
    // The bulk values are tiny; outliers must not inflate the width.
    assert!(data.len() < 1024, "outliers should escape, got {} bytes", data.len());
    for (b, entry) in info.index.iter().enumerate() {
        let block = decode_block_at(&data, b as u32, &limits()).unwrap();
        let start = entry.first_value_index as usize;
        assert_eq!(block, &values[start..(start + 64).min(values.len())]);
    }
}

#[test]
fn full_i64_range_random_roundtrip() {
    let mut rng = Rng(0x1234_5678_9abc_def0);
    let values: Vec<i64> = (0..2000).map(|_| rng.next() as i64).collect();
    roundtrip(&values, 111);
}

#[test]
fn bounded_random_roundtrip_across_widths() {
    let mut rng = Rng(0xdead_beef_cafe_f00d);
    for bits in [1u32, 7, 8, 16, 31, 32, 48, 63] {
        let mask = (1u64 << bits) - 1;
        let values: Vec<i64> = (0..500)
            .map(|_| {
                let magnitude = (rng.next() & mask) as i64;
                if rng.next() & 1 == 0 { magnitude } else { -magnitude }
            })
            .collect();
        roundtrip(&values, 64);
    }
}

#[test]
fn random_with_sparse_outliers() {
    let mut rng = Rng(0x0bad_5eed_0bad_5eed);
    let mut values: Vec<i64> = (0..1000).map(|_| (rng.next() % 200) as i64 - 100).collect();
    for _ in 0..10 {
        values[(rng.next() % 1000) as usize] = rng.next() as i64;
    }
    roundtrip(&values, 50);
}

#[test]
fn block_size_boundaries() {
    let values: Vec<i64> = (0..257).collect();
    for block_size in [1u32, 2, 127, 128, 256, 1000] {
        roundtrip(&values, block_size);
    }
}

#[test]
fn single_block_access_matches_slice() {
    let values: Vec<i64> = (0..777).map(|i| i * 3 - 1000).collect();
    let data = encode_stream(&values, 100).unwrap();
    let info = inspect(&data, &limits()).unwrap();
    assert_eq!(info.header.block_count, 8);
    for (b, entry) in info.index.iter().enumerate() {
        let block = decode_block_at(&data, b as u32, &limits()).unwrap();
        let start = entry.first_value_index as usize;
        let end = (start + 100).min(values.len());
        assert_eq!(block, &values[start..end]);
    }
}
