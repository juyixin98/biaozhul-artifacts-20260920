//! Robustness tests: malformed headers and hostile inputs must produce
//! errors, never panics, shift overflows, or runaway allocations.

use bitpack::error::Error;
use bitpack::limits::Limits;
use bitpack::{decode_block_at, decode_stream, encode_stream, inspect};

fn limits() -> Limits {
    Limits::default()
}

/// Build a valid one-block stream of `values` for mutation tests.
fn sample_stream() -> Vec<u8> {
    encode_stream(&[1, -2, 3, -4, 5], 16).unwrap()
}

#[test]
fn bad_magic_is_rejected() {
    let mut data = sample_stream();
    data[0] = b'X';
    assert!(matches!(decode_stream(&data, &limits()), Err(Error::InvalidMagic)));
}

#[test]
fn truncated_inputs_never_panic() {
    let data = sample_stream();
    for len in 0..data.len() {
        let _ = decode_stream(&data[..len], &limits());
        let _ = inspect(&data[..len], &limits());
        let _ = decode_block_at(&data[..len], 0, &limits());
    }
}

#[test]
fn every_header_byte_mutation_is_handled() {
    let data = sample_stream();
    for pos in 0..32 {
        for delta in [1u8, 0x80, 0xff] {
            let mut mutated = data.clone();
            mutated[pos] = mutated[pos].wrapping_add(delta);
            // Must return (Ok or Err), never panic.
            let _ = decode_stream(&mutated, &limits());
            let _ = inspect(&mutated, &limits());
        }
    }
}

#[test]
fn bit_width_above_64_is_rejected_without_shift_overflow() {
    let data = sample_stream();
    let block_offset = 32usize; // first block starts right after the header
    for bad_width in [65u8, 100, 200, 255] {
        let mut mutated = data.clone();
        mutated[block_offset + 8] = bad_width;
        assert!(
            matches!(
                decode_stream(&mutated, &limits()),
                Err(Error::InvalidBitWidth(_)) | Err(Error::Corrupt(_))
            ),
            "width {bad_width} must be rejected"
        );
    }
}

#[test]
fn huge_value_count_cannot_force_huge_allocation() {
    let mut data = sample_stream();
    let block_offset = 32usize;
    // Claim 4 billion values in the block header.
    data[block_offset + 9..block_offset + 13].copy_from_slice(&u32::MAX.to_le_bytes());
    let result = decode_stream(&data, &limits());
    assert!(matches!(
        result,
        Err(Error::LimitExceeded(_)) | Err(Error::Corrupt(_))
    ));
}

#[test]
fn huge_total_values_is_capped_by_limits() {
    let mut data = sample_stream();
    data[16..24].copy_from_slice(&u64::MAX.to_le_bytes());
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::LimitExceeded(_)) | Err(Error::Corrupt(_))
    ));
}

#[test]
fn exception_count_above_value_count_is_rejected() {
    let mut data = sample_stream();
    let block_offset = 32usize;
    data[block_offset + 13..block_offset + 17].copy_from_slice(&u32::MAX.to_le_bytes());
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::Corrupt(_)) | Err(Error::LimitExceeded(_))
    ));
}

#[test]
fn packed_len_mismatch_is_rejected() {
    let mut data = sample_stream();
    let block_offset = 32usize;
    data[block_offset + 17..block_offset + 21].copy_from_slice(&12345u32.to_le_bytes());
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::Corrupt(_))
    ));
}

#[test]
fn index_offset_out_of_range_is_rejected() {
    let mut data = sample_stream();
    data[24..32].copy_from_slice(&u64::MAX.to_le_bytes());
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::Corrupt(_)) | Err(Error::UnexpectedEof)
    ));
}

#[test]
fn trailing_bytes_are_rejected() {
    let mut data = sample_stream();
    data.push(0);
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::TrailingBytes)
    ));
}

#[test]
fn unsupported_version_and_flags_are_rejected() {
    let mut data = sample_stream();
    data[4] = 2;
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::UnsupportedVersion(2))
    ));
    let mut data = sample_stream();
    data[6] = 1; // big-endian flag does not exist
    assert!(matches!(
        decode_stream(&data, &limits()),
        Err(Error::UnsupportedFlags(1))
    ));
}

#[test]
fn random_garbage_never_panics() {
    let mut state = 0x243f_6a88_85a3_08d3u64;
    for len in [0usize, 1, 31, 32, 33, 100, 1000] {
        for _ in 0..20 {
            let bytes: Vec<u8> = (0..len)
                .map(|_| {
                    state ^= state << 13;
                    state ^= state >> 7;
                    state ^= state << 17;
                    state as u8
                })
                .collect();
            let _ = decode_stream(&bytes, &limits());
            let _ = inspect(&bytes, &limits());
            let _ = decode_block_at(&bytes, 0, &limits());
        }
    }
}

#[test]
fn block_number_out_of_range_is_rejected() {
    let data = sample_stream();
    assert!(matches!(
        decode_block_at(&data, 99, &limits()),
        Err(Error::Corrupt(_))
    ));
}

#[test]
fn output_limit_is_enforced() {
    let values: Vec<i64> = (0..1000).collect();
    let data = encode_stream(&values, 100).unwrap();
    let tight = Limits {
        max_output_values: 10,
        ..Limits::default()
    };
    assert!(matches!(
        decode_stream(&data, &tight),
        Err(Error::LimitExceeded(_))
    ));
    // But a single block is still reachable under the same output limit.
    let block = decode_block_at(&data, 0, &tight).unwrap();
    assert_eq!(block.len(), 100);
}
