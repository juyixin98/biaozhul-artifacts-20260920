//! Corrupt-input tests: malformed headers and blocks must produce errors, not
//! panics, shift overflows, or out-of-bounds allocations.

use bpb::decode::{decode_all, decode_block, inspect, Limits};
use bpb::encode::{encode, EncodeOptions};
use bpb::Error;

/// A valid small stream to mutate. Layout: header 24 bytes, block at 24,
/// index at the end.
fn sample() -> Vec<u8> {
    encode(&[1, 2, 3, 4], &EncodeOptions { block_size: 4 }).unwrap()
}

fn set_u32(buf: &mut [u8], at: usize, v: u32) {
    buf[at..at + 4].copy_from_slice(&v.to_le_bytes());
}

fn set_u64(buf: &mut [u8], at: usize, v: u64) {
    buf[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

#[test]
fn bad_magic() {
    let mut blob = sample();
    blob[0] = b'X';
    assert!(matches!(decode_all(&blob, &Limits::default()), Err(Error::BadMagic)));
}

#[test]
fn truncated_everywhere() {
    let blob = sample();
    // Every strict prefix must fail cleanly (never panic).
    for len in 0..blob.len() {
        assert!(decode_all(&blob[..len], &Limits::default()).is_err(), "len {len}");
    }
}

#[test]
fn unsupported_version() {
    let mut blob = sample();
    blob[4] = 99;
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::UnsupportedVersion(99))
    ));
}

#[test]
fn nonzero_flags_rejected() {
    let mut blob = sample();
    blob[5] = 1;
    assert!(matches!(decode_all(&blob, &Limits::default()), Err(Error::InvalidFlags)));
}

#[test]
fn huge_block_count_cannot_force_allocation() {
    let mut blob = sample();
    set_u32(&mut blob, 8, u32::MAX);
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::LimitExceeded("block count"))
    ));
}

#[test]
fn huge_total_values_cannot_force_allocation() {
    let mut blob = sample();
    set_u64(&mut blob, 12, u64::MAX);
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::LimitExceeded("total values"))
    ));
}

#[test]
fn output_limit_enforced() {
    let blob = sample();
    let limits = Limits {
        max_output_values: 3,
        ..Limits::default()
    };
    assert!(matches!(
        decode_all(&blob, &limits),
        Err(Error::LimitExceeded("output values"))
    ));
}

#[test]
fn index_offset_out_of_range() {
    let mut blob = sample();
    set_u64(&mut blob, 20, u64::MAX);
    assert!(decode_all(&blob, &Limits::default()).is_err());
    set_u64(&mut blob, 20, 5); // inside the header
    assert!(decode_all(&blob, &Limits::default()).is_err());
}

#[test]
fn bit_width_above_64_rejected_before_any_shift() {
    let mut blob = sample();
    // Block header starts at 28: count(4) base(8) bit_width(1) at offset 40.
    blob[40] = 200;
    // Give the buffer plenty of trailing bytes so the failure must come from
    // validation, not truncation.
    blob.extend_from_slice(&[0u8; 4096]);
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn escape_count_above_block_count_rejected() {
    let mut blob = sample();
    // escape_count at offset 28 + 13 = 41.
    set_u32(&mut blob, 41, 5); // count is 4
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn escape_index_out_of_range_rejected() {
    // [i64::MIN, i64::MAX] forces one escape entry (delta overflows i64).
    let mut blob = encode(&[i64::MIN, i64::MAX], &EncodeOptions { block_size: 2 }).unwrap();
    let info = inspect(&blob, &Limits::default()).unwrap();
    assert_eq!(info.blocks[0].escape_count, 1);
    let packed_len = (2 * info.blocks[0].bit_width as usize).div_ceil(8);
    let escape_at = 28 + 17 + packed_len;
    set_u32(&mut blob, escape_at, 2); // valid indices are 0..=1
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn delta_overflow_rejected() {
    // Hand-build a stream: count=1, base=i64::MAX, bit_width=64, packed
    // zigzag(10) = 20. base + 10 overflows i64 and must be an error.
    let mut blob = Vec::new();
    blob.extend_from_slice(b"BPB1");
    blob.push(1); // version
    blob.push(0); // flags
    blob.extend_from_slice(&0u16.to_le_bytes());
    blob.extend_from_slice(&1u32.to_le_bytes()); // block_count
    blob.extend_from_slice(&1u64.to_le_bytes()); // total_values
    blob.extend_from_slice(&53u64.to_le_bytes()); // index_offset = 28 + 17 + 8
    // block
    blob.extend_from_slice(&1u32.to_le_bytes()); // count
    blob.extend_from_slice(&i64::MAX.to_le_bytes()); // base
    blob.push(64); // bit_width
    blob.extend_from_slice(&0u32.to_le_bytes()); // escape_count
    blob.extend_from_slice(&20u64.to_le_bytes()); // packed: zigzag(10)
    // index
    blob.extend_from_slice(&28u64.to_le_bytes()); // block offset
    blob.extend_from_slice(&0u64.to_le_bytes()); // first_value
    blob.extend_from_slice(&1u32.to_le_bytes()); // count
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn index_first_value_not_cumulative_rejected() {
    let values: Vec<i64> = (0..10).collect();
    let mut blob = encode(&values, &EncodeOptions { block_size: 5 }).unwrap();
    // Second index entry starts at index_offset + 20; its first_value must be 5.
    let info = inspect(&blob, &Limits::default()).unwrap();
    assert_eq!(info.block_count, 2);
    let second = info.index_offset as usize + 20;
    set_u64(&mut blob, second + 8, 4);
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn block_offset_outside_block_region_rejected() {
    let mut blob = sample();
    let info = inspect(&blob, &Limits::default()).unwrap();
    let index_at = info.index_offset as usize;
    set_u64(&mut blob, index_at, 2); // offset inside the header
    assert!(matches!(
        decode_all(&blob, &Limits::default()),
        Err(Error::InvalidData(_))
    ));
}

#[test]
fn corrupt_input_never_panics_on_block_decode() {
    let blob = sample();
    // Byte-level fuzz: flip each byte in turn; decode must return, not panic.
    for i in 0..blob.len() {
        let mut mutated = blob.clone();
        mutated[i] ^= 0xff;
        let _ = decode_all(&mutated, &Limits::default());
        let _ = decode_block(&mutated, 0, &Limits::default());
        let _ = inspect(&mutated, &Limits::default());
    }
}
