//! Roundtrip tests: encode -> decode must reproduce the input exactly,
//! including boundary bit widths 0 and 64, negative values, and extreme
//! escape values.

use bpb::decode::{decode_all, decode_block, inspect, Limits};
use bpb::encode::{encode, EncodeOptions};

fn roundtrip(values: &[i64], block_size: u32) -> Vec<u8> {
    let blob = encode(values, &EncodeOptions { block_size }).unwrap();
    let back = decode_all(&blob, &Limits::default()).unwrap();
    assert_eq!(back, values, "roundtrip failed for {values:?}");
    blob
}

/// Simple deterministic xorshift64* generator for reproducible "random" data.
struct XorShift(u64);
impl XorShift {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

#[test]
fn empty_input() {
    roundtrip(&[], 128);
}

#[test]
fn single_value() {
    roundtrip(&[42], 128);
    roundtrip(&[i64::MIN], 128);
    roundtrip(&[i64::MAX], 128);
}

#[test]
fn constant_values_use_bit_width_zero() {
    let values = vec![-7i64; 100];
    let blob = roundtrip(&values, 128);
    let info = inspect(&blob, &Limits::default()).unwrap();
    assert_eq!(info.blocks[0].bit_width, 0);
    assert_eq!(info.blocks[0].escape_count, 0);
}

#[test]
fn negative_and_mixed_signs() {
    let values: Vec<i64> = (-50..50).collect();
    roundtrip(&values, 128);
}

#[test]
fn extreme_values_become_escapes() {
    // base = i64::MIN, so the delta to i64::MAX (2^64 - 1) overflows i64 and
    // must go through the escape table.
    let values = [i64::MIN, i64::MAX, 0, -1, 1, i64::MIN + 1, i64::MAX - 1];
    let blob = roundtrip(&values, 128);
    let info = inspect(&blob, &Limits::default()).unwrap();
    assert!(info.blocks[0].escape_count >= 1);
}

#[test]
fn outliers_escape_instead_of_widening() {
    // Mostly tiny deltas with two huge outliers: the encoder should keep a
    // small bit width and escape the outliers.
    let mut values = vec![0i64; 64];
    values[10] = i64::MAX;
    values[55] = i64::MIN;
    let blob = roundtrip(&values, 128);
    let info = inspect(&blob, &Limits::default()).unwrap();
    assert_eq!(info.blocks[0].escape_count, 2);
    assert!(info.blocks[0].bit_width <= 8);
}

#[test]
fn full_range_random_uses_wide_or_escape_paths() {
    let mut rng = XorShift(0x1234_5678_9abc_def0);
    let values: Vec<i64> = (0..1000).map(|_| rng.next() as i64).collect();
    roundtrip(&values, 128);
}

#[test]
fn bit_width_sweep() {
    // Deltas bounded to exercise exact bit widths from 0 through 63; width 64
    // is covered by full_range_random_uses_wide_or_escape_paths.
    for bits in [0u32, 1, 2, 3, 7, 8, 15, 16, 31, 32, 47, 63] {
        let bound: i64 = if bits == 0 { 0 } else { 1i64 << (bits - 1) };
        let range = 2u64 * bound as u64; // bound <= 2^62, so this fits u64
        let mut rng = XorShift(0xdead_beef + u64::from(bits));
        let values: Vec<i64> = (0..200)
            .map(|_| {
                if bound == 0 {
                    1000
                } else {
                    1000 + (rng.next() % range) as i64 - bound
                }
            })
            .collect();
        roundtrip(&values, 256);
    }
}

#[test]
fn multi_block_and_single_block_index() {
    let mut rng = XorShift(42);
    let values: Vec<i64> = (0..1000).map(|_| (rng.next() % 10_000) as i64 - 5000).collect();
    let blob = roundtrip(&values, 100);

    let info = inspect(&blob, &Limits::default()).unwrap();
    assert_eq!(info.block_count, 10);
    assert_eq!(info.total_values, 1000);

    // The index must locate every block individually.
    for (i, entry) in info.index.iter().enumerate() {
        let block = decode_block(&blob, i as u32, &Limits::default()).unwrap();
        let start = entry.first_value as usize;
        let end = start + entry.count as usize;
        assert_eq!(block, &values[start..end], "block {i} mismatch");
    }
}

#[test]
fn block_index_out_of_range_is_an_error() {
    let blob = encode(&[1, 2, 3], &EncodeOptions::default()).unwrap();
    assert!(decode_block(&blob, 1, &Limits::default()).is_err());
}

#[test]
fn every_block_size_boundary() {
    let values: Vec<i64> = (0..=256).map(|i| i * 3 - 100).collect();
    for block_size in [1u32, 2, 3, 127, 128, 255, 256] {
        roundtrip(&values, block_size);
    }
}

#[test]
fn invalid_block_size_rejected() {
    assert!(encode(&[1], &EncodeOptions { block_size: 0 }).is_err());
    assert!(encode(&[1], &EncodeOptions { block_size: 257 }).is_err());
}
