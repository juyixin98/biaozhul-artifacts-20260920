//! Integration tests: full-container round trips through the public API,
//! erasure-combination enumeration, and the documented failure modes.

use ecstripe::codec::{self, EncodeParams};
use ecstripe::format;
use ecstripe::Error;

/// Deterministic pseudo-random bytes (xorshift32), so tests need no rand crate.
fn test_data(len: usize, seed: u32) -> Vec<u8> {
    let mut x = seed.max(1);
    (0..len)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            (x >> 8) as u8
        })
        .collect()
}

fn params(k: u8, m: u8, stripe_size: u32) -> EncodeParams {
    EncodeParams {
        data_shards: k,
        parity_shards: m,
        stripe_size,
        ..EncodeParams::default()
    }
}

fn encode_to_vec(p: &EncodeParams, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let stats = codec::encode(p, data.len() as u64, data, &mut out).unwrap();
    assert_eq!(out.len() as u64, stats.output_bytes);
    out
}

fn decode_from_vec(container: &[u8], missing: &[u32], max_output: u64) -> Result<Vec<u8>, Error> {
    let mut out = Vec::new();
    codec::decode(container, &mut out, missing, max_output).map(|_| out)
}

/// All subsets of `n` shard indices of size `size`.
fn subsets(n: usize, size: usize) -> Vec<Vec<u32>> {
    let mut out = Vec::new();
    let mut current = Vec::with_capacity(size);
    fn go(
        start: usize,
        n: usize,
        size: usize,
        current: &mut Vec<u32>,
        out: &mut Vec<Vec<u32>>,
    ) {
        if current.len() == size {
            out.push(current.clone());
            return;
        }
        for i in start..n {
            current.push(i as u32);
            go(i + 1, n, size, current, out);
            current.pop();
        }
    }
    go(0, n, size, &mut current, &mut out);
    out
}

#[test]
fn roundtrip_no_loss_various_sizes() {
    // Sizes chosen to hit: empty, sub-stripe, exact stripe, multi-stripe
    // with a ragged tail.
    for &len in &[0usize, 1, 100, 4 * 64, 4 * 64 * 3 + 17] {
        let p = params(4, 2, 64);
        let data = test_data(len, 7);
        let container = encode_to_vec(&p, &data);
        let back = decode_from_vec(&container, &[], u64::MAX.min(format::HARD_MAX_OUTPUT_BYTES))
            .unwrap();
        assert_eq!(back, data, "len={len}");
    }
}

#[test]
fn enumerate_all_erasure_combinations_recover() {
    // Acceptance criterion: for small parameter sets, EVERY combination of
    // up to m missing shards must decode back to the exact original.
    for &(k, m) in &[(2u8, 1u8), (4, 2), (3, 3)] {
        let p = params(k, m, 48);
        let data = test_data(1000, 42); // spans multiple stripes with padding
        let container = encode_to_vec(&p, &data);
        let n = (k + m) as usize;

        let mut combos = 0usize;
        for erased in 0..=(m as usize) {
            for missing in subsets(n, erased) {
                let back = decode_from_vec(&container, &missing, 1 << 20)
                    .unwrap_or_else(|e| panic!("k={k} m={m} missing={missing:?}: {e}"));
                assert_eq!(back, data, "k={k} m={m} missing={missing:?}");
                combos += 1;
            }
        }
        // Sanity: we really enumerated C(n,0)+..+C(n,m) combinations.
        let expect: usize = (0..=(m as usize)).map(|e| subsets(n, e).len()).sum();
        assert_eq!(combos, expect);
        assert!(combos > 1);
    }
}

#[test]
fn too_many_missing_shards_fail() {
    let p = params(4, 2, 64);
    let data = test_data(500, 9);
    let container = encode_to_vec(&p, &data);

    // m+1 = 3 erasures must be rejected, whether they hit data or parity.
    for missing in [vec![0, 1, 2], vec![0, 4, 5], vec![3, 4, 5]] {
        let err = decode_from_vec(&container, &missing, 1 << 20).unwrap_err();
        assert!(
            matches!(err, Error::TooManyErasures { .. }),
            "missing={missing:?}: {err}"
        );
    }
}

#[test]
fn invalid_shard_indices_fail() {
    let p = params(2, 1, 32);
    let data = test_data(100, 5);
    let container = encode_to_vec(&p, &data);
    // Out of range (n = 3) and duplicated indices.
    for missing in [vec![3], vec![0, 0], vec![255]] {
        let err = decode_from_vec(&container, &missing, 1 << 20).unwrap_err();
        assert!(matches!(err, Error::InvalidShardIndex(_)), "missing={missing:?}: {err}");
    }
}

#[test]
fn corrupt_block_length_prefix_fails() {
    let p = params(4, 2, 64);
    let data = test_data(300, 11);
    let mut container = encode_to_vec(&p, &data);

    // First block's length prefix starts right after the 20-byte header.
    let prefix_at = format::HEADER_LEN;
    container[prefix_at] ^= 0x01;
    let err = decode_from_vec(&container, &[], 1 << 20).unwrap_err();
    assert!(
        matches!(err, Error::LengthMismatch { .. }),
        "expected LengthMismatch, got: {err}"
    );
}

#[test]
fn truncated_container_fails() {
    let p = params(4, 2, 64);
    let data = test_data(300, 13);
    let container = encode_to_vec(&p, &data);

    for cut in [10, format::HEADER_LEN + 2, container.len() - 1] {
        let err = decode_from_vec(&container[..cut], &[], 1 << 20).unwrap_err();
        assert!(
            matches!(err, Error::CorruptFormat(_) | Error::LengthMismatch { .. }),
            "cut={cut}: {err}"
        );
    }
}

#[test]
fn trailing_bytes_fail() {
    let p = params(2, 1, 32);
    let data = test_data(50, 17);
    let mut container = encode_to_vec(&p, &data);
    container.push(0xAA);
    let err = decode_from_vec(&container, &[], 1 << 20).unwrap_err();
    assert!(matches!(err, Error::CorruptFormat(_)), "{err}");
}

#[test]
fn tampered_original_len_fails() {
    let p = params(2, 1, 32);
    let data = test_data(50, 19);
    let mut container = encode_to_vec(&p, &data);

    // Inflate original_len in the header: the body no longer matches.
    let big = 10_000u64.to_le_bytes();
    container[12..20].copy_from_slice(&big);
    let err = decode_from_vec(&container, &[], 1 << 20).unwrap_err();
    assert!(
        matches!(err, Error::CorruptFormat(_) | Error::LengthMismatch { .. }),
        "{err}"
    );
}

#[test]
fn decode_output_limit_enforced() {
    let p = params(2, 1, 32);
    let data = test_data(1000, 23);
    let container = encode_to_vec(&p, &data);

    let err = decode_from_vec(&container, &[], 999).unwrap_err();
    assert!(
        matches!(err, Error::OutputLimitExceeded { needed: 1000, limit: 999 }),
        "{err}"
    );
    // Exactly the needed size must pass.
    assert_eq!(decode_from_vec(&container, &[], 1000).unwrap(), data);
}

#[test]
fn silent_corruption_is_not_detected_without_external_check() {
    // Documented limitation: flipping a byte inside a shard does NOT make
    // decode fail; the output is silently wrong. Only an external checksum
    // (or marking the shard as a known erasure) saves you.
    let p = params(4, 2, 64);
    let data = test_data(500, 29);
    let mut container = encode_to_vec(&p, &data);

    // Corrupt one byte inside data shard 0's first block (after header +
    // 4-byte length prefix).
    let byte_at = format::HEADER_LEN + 4 + 10;
    container[byte_at] ^= 0xFF;

    let decoded = decode_from_vec(&container, &[], 1 << 20).unwrap();
    assert_ne!(decoded, data, "silent corruption must pass through undetected");
    assert_eq!(decoded.len(), data.len());

    // Treating the corrupted shard as a known erasure DOES recover, because
    // the remaining k shards are intact.
    let recovered = decode_from_vec(&container, &[0], 1 << 20).unwrap();
    assert_eq!(recovered, data);
}

#[test]
fn parameter_limits_enforced_on_encode() {
    let data = test_data(10, 31);

    // stripe_size = 0
    let p = EncodeParams { stripe_size: 0, ..params(2, 1, 32) };
    assert!(matches!(
        codec::encode(&p, 10, &data[..], Vec::new()),
        Err(Error::InvalidParams(_))
    ));

    // k = 0 / m = 0
    assert!(matches!(
        codec::encode(&params(0, 1, 32), 10, &data[..], Vec::new()),
        Err(Error::InvalidParams(_))
    ));
    assert!(matches!(
        codec::encode(&params(1, 0, 32), 10, &data[..], Vec::new()),
        Err(Error::InvalidParams(_))
    ));

    // k + m > 255
    assert!(matches!(
        codec::encode(&params(200, 100, 32), 10, &data[..], Vec::new()),
        Err(Error::InvalidParams(_))
    ));

    // (k+m) * stripe_size over the memory cap
    let p = EncodeParams { max_stripe_memory: 1024, ..params(4, 2, 512) };
    assert!(matches!(
        codec::encode(&p, 10, &data[..], Vec::new()),
        Err(Error::InvalidParams(_))
    ));
}

#[test]
fn hostile_header_limits_enforced_on_decode() {
    // A hand-crafted container claiming an oversized stripe_size must be
    // rejected by the decoder's limit checks, not trusted.
    let header = format::Header {
        data_shards: 4,
        parity_shards: 2,
        stripe_size: (format::MAX_STRIPE_SIZE as u32) + 1,
        original_len: 1,
    };
    let mut buf = Vec::new();
    header.write_to(&mut buf).unwrap();
    let err = decode_from_vec(&buf, &[], 1 << 20).unwrap_err();
    assert!(matches!(err, Error::InvalidParams(_)), "{err}");

    // Same for a stripe that exceeds the per-stripe memory cap.
    let header = format::Header {
        data_shards: 200,
        parity_shards: 55,
        stripe_size: 1 << 20, // 255 MiB per stripe > 64 MiB cap
        original_len: 1,
    };
    let mut buf = Vec::new();
    header.write_to(&mut buf).unwrap();
    let err = decode_from_vec(&buf, &[], 1 << 20).unwrap_err();
    assert!(matches!(err, Error::InvalidParams(_)), "{err}");
}

#[test]
fn bad_magic_and_version_fail() {
    let p = params(2, 1, 32);
    let data = test_data(64, 37);

    let mut container = encode_to_vec(&p, &data);
    container[0] = b'X';
    assert!(matches!(
        decode_from_vec(&container, &[], 1 << 20),
        Err(Error::BadMagic)
    ));

    let mut container = encode_to_vec(&p, &data);
    container[4] = 99;
    assert!(matches!(
        decode_from_vec(&container, &[], 1 << 20),
        Err(Error::UnsupportedVersion(99))
    ));
}
