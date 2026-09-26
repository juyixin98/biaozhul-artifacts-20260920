//! End-to-end and acceptance tests for the adaptive arithmetic codec.
//!
//! Coverage: empty input, every byte value, skewed long streams with repeated
//! rescaling, random fuzz round-trips, deterministic output, truncated and
//! corrupted streams, output-length caps, and stream-vs-buffer equivalence.

use aac::coder::{decode_bytes, encode_bytes};
use aac::container::{pack, pack_stream, unpack, unpack_stream};
use aac::{Error, DEFAULT_MAX_OUTPUT};

/// Deterministic 64-bit LCG so fuzz data is reproducible without dependencies.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed)
    }
    fn next_u64(&mut self) -> u64 {
        // Numerical Recipes constants.
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn byte(&mut self) -> u8 {
        (self.next_u64() >> 33) as u8
    }
}

fn roundtrip_buffer(data: &[u8]) {
    let (blob, enc_stats) = pack(data, None).expect("encode");
    let (back, dec_stats) = unpack(&blob, None).expect("decode");
    assert_eq!(back, data, "buffer roundtrip mismatch");
    assert_eq!(enc_stats.input_bytes as usize, data.len());
    assert_eq!(dec_stats.output_bytes as usize, data.len());
    assert_eq!(enc_stats.rescales, dec_stats.rescales);
}

fn roundtrip_stream(data: &[u8]) {
    let mut blob = Vec::new();
    let es = pack_stream(data, &mut blob, None).expect("stream encode");
    let mut back = Vec::new();
    let ds = unpack_stream(blob.as_slice(), &mut back, None).expect("stream decode");
    assert_eq!(back, data, "stream roundtrip mismatch");
    assert_eq!(es.rescales, ds.rescales);

    // The streaming container must be byte-identical to the buffered one.
    let (buffered, _) = pack(data, None).unwrap();
    assert_eq!(blob, buffered, "stream and buffer containers diverge");
}

#[test]
fn empty_input_roundtrips() {
    roundtrip_buffer(b"");
    roundtrip_stream(b"");
}

#[test]
fn each_single_byte_roundtrips() {
    for v in 0u16..=255 {
        let data = [v as u8];
        roundtrip_buffer(&data);
        roundtrip_stream(&data);
    }
}

#[test]
fn all_byte_values_once_and_repeated() {
    let once: Vec<u8> = (0u16..=255).map(|v| v as u8).collect();
    roundtrip_buffer(&once);
    roundtrip_stream(&once);

    let mut repeated = Vec::with_capacity(256 * 7);
    for _ in 0..7 {
        repeated.extend_from_slice(&once);
    }
    roundtrip_buffer(&repeated);
    roundtrip_stream(&repeated);
}

#[test]
fn long_skewed_data_triggers_many_rescales() {
    // 99.9% zero bytes: the model saturates and rescales repeatedly.
    let mut rng = Rng::new(0x1234_5678);
    let n = 200_000usize;
    let data: Vec<u8> = (0..n)
        .map(|_| {
            if rng.next_u64() % 1000 == 0 {
                rng.byte()
            } else {
                0
            }
        })
        .collect();
    let (blob, stats) = pack(&data, None).unwrap();
    assert!(
        stats.rescales >= 20,
        "expected many rescales on skewed data, got {}",
        stats.rescales
    );
    let (back, dstats) = unpack(&blob, None).unwrap();
    assert_eq!(back, data);
    assert_eq!(dstats.rescales, stats.rescales);
    // Compression actually beats one-byte-per-symbol for the skew.
    assert!(
        blob.len() < data.len(),
        "skewed data not compressed: {} -> {}",
        data.len(),
        blob.len()
    );
    roundtrip_stream(&data);
}

#[test]
fn skewed_runs_of_every_symbol_rescale() {
    // A long run of a single value forces rescale saturation too.
    let data = vec![0xA5u8; 100_000];
    let (blob, stats) = pack(&data, None).unwrap();
    assert!(stats.rescales >= 10, "rescales={}", stats.rescales);
    let (back, _) = unpack(&blob, None).unwrap();
    assert_eq!(back, data);
}

#[test]
fn random_fuzz_roundtrips() {
    for seed in 0u64..40 {
        let mut rng = Rng::new(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(1));
        let len = (rng.next_u64() % 5_000) as usize;
        // Vary bias: sometimes uniform, sometimes skewed.
        let mode = rng.next_u64() % 3;
        let data: Vec<u8> = (0..len)
            .map(|_| match mode {
                0 => rng.byte(),
                1 => {
                    if rng.next_u64() % 4 == 0 {
                        rng.byte()
                    } else {
                        b'X'
                    }
                }
                _ => (rng.next_u64() % 4) as u8,
            })
            .collect();
        roundtrip_buffer(&data);
        if seed % 5 == 0 {
            roundtrip_stream(&data);
        }
    }
}

#[test]
fn output_is_deterministic() {
    let mut rng = Rng::new(42);
    let data: Vec<u8> = (0..10_000).map(|_| rng.byte()).collect();
    let (a, sa) = pack(&data, None).unwrap();
    let (b, sb) = pack(&data, None).unwrap();
    assert_eq!(a, b, "encoding the same input twice must be identical");
    assert_eq!(sa.coded_bits, sb.coded_bits);
}

#[test]
fn every_truncated_prefix_is_rejected() {
    let data = b"truncate me, please, in every possible way!!";
    let (blob, _) = pack(data, None).unwrap();
    for cut in 0..blob.len() {
        match unpack(&blob[..cut], None) {
            Err(_) => {}
            Ok((out, _)) => panic!(
                "prefix of length {cut} accepted, decoded {} bytes",
                out.len()
            ),
        }
    }
}

#[test]
fn every_truncated_prefix_rejected_via_stream() {
    let data: Vec<u8> = (0..2_000u32).map(|i| (i % 7) as u8).collect();
    let mut blob = Vec::new();
    pack_stream(data.as_slice(), &mut blob, None).unwrap();
    for cut in (0..blob.len()).step_by(7) {
        let mut out = Vec::new();
        assert!(
            unpack_stream(&blob[..cut], &mut out, None).is_err(),
            "stream prefix of length {cut} unexpectedly accepted"
        );
    }
}

#[test]
fn raw_coded_payload_truncated_decoder_errors_on_short_prefixes() {
    let data = b"payload truncation without the container footer";
    let (payload, _) = encode_bytes(data, None).unwrap();
    // Aggressive cuts (well before the coder's flush tail) must fail.
    for cut in 0..payload.len().saturating_sub(8) {
        let r = decode_bytes(&payload[..cut], DEFAULT_MAX_OUTPUT);
        assert!(r.is_err(), "raw payload prefix {cut} unexpectedly decoded");
    }
    // The full payload round-trips.
    let (back, _) = decode_bytes(&payload, DEFAULT_MAX_OUTPUT).unwrap();
    assert_eq!(back, data);
}

#[test]
fn corrupted_byte_anywhere_is_rejected() {
    let data: Vec<u8> = (0..500u32).map(|i| (i * 31) as u8).collect();
    let (mut blob, _) = pack(&data, None).unwrap();
    for pos in (0..blob.len()).step_by(3) {
        let original = blob[pos];
        blob[pos] ^= 0xA5;
        if blob[pos] == original {
            blob[pos] ^= 0xFF;
        }
        assert!(
            unpack(&blob, None).is_err(),
            "corruption at byte {pos} not detected"
        );
        blob[pos] = original;
    }
    let (back, _) = unpack(&blob, None).unwrap();
    assert_eq!(back, data);
}

#[test]
fn decode_output_limit_is_enforced() {
    let data = vec![7u8; 10_000];
    let (blob, _) = pack(&data, None).unwrap();
    for cap in [0u64, 1, 99, 9_999] {
        match unpack(&blob, Some(cap)) {
            Err(Error::OutputLimitExceeded { .. }) => {}
            other => panic!("cap {cap}: expected OutputLimitExceeded, got {other:?}"),
        }
    }
    // Cap equal to the length succeeds.
    let (back, _) = unpack(&blob, Some(10_000)).unwrap();
    assert_eq!(back.len(), 10_000);
}

#[test]
fn encode_payload_limit_is_enforced() {
    let data = vec![0u8; 5_000];
    // A cap smaller than any possible coded payload (header+footer = 29).
    let result = pack(&data, Some(5));
    assert!(matches!(result, Err(Error::OutputLimitExceeded { .. })));
}

#[test]
fn forged_length_field_above_cap_rejected() {
    let data = b"x";
    let (good, _) = pack(data, None).unwrap();

    // Build a container with a valid CRC but a lying original_length.
    let mut forged = good[..good.len() - 16].to_vec(); // prefix+payload+coded_bits
    forged.extend_from_slice(&999u64.to_be_bytes());
    let crc = aac::Crc32::checksum(&forged);
    forged.extend_from_slice(&crc.to_be_bytes());
    forged.extend_from_slice(b"ACED");

    match unpack(&forged, Some(50)) {
        Err(Error::OutputLimitExceeded { .. }) => {}
        other => panic!("lying length with small cap: {other:?}"),
    }

    // A tampered container (bad CRC) is also rejected.
    let (mut blob, _) = pack(data, None).unwrap();
    let len_pos = blob.len() - 24 + 8;
    blob[len_pos..len_pos + 8].copy_from_slice(&10_000_000u64.to_be_bytes());
    assert!(unpack(&blob, None).is_err());
}

#[test]
fn non_zero_padding_bits_rejected() {
    let (mut blob, _) = pack(b"pad", None).unwrap();
    // Flip a low padding bit of the last payload byte (byte before footer).
    let payload_last = blob.len() - 24 - 1;
    blob[payload_last] |= 0x01;
    // Recompute CRC so the padding check is the one that fires.
    let crc = aac::Crc32::checksum(&blob[..blob.len() - 8]);
    let crc_start = blob.len() - 8;
    blob[crc_start..crc_start + 4].copy_from_slice(&crc.to_be_bytes());
    assert!(matches!(unpack(&blob, None), Err(Error::InvalidFormat(_))));
}

#[test]
fn empty_or_garbage_container_rejected() {
    assert!(unpack(&[], None).is_err());
    assert!(unpack(b"short", None).is_err());
    assert!(unpack(&[0u8; 64], None).is_err());
    let (mut blob, _) = pack(b"ok", None).unwrap();
    blob[0] = b'X';
    assert!(unpack(&blob, None).is_err());
    let (mut blob, _) = pack(b"ok", None).unwrap();
    blob[4] = 99;
    assert!(unpack(&blob, None).is_err());
}

#[test]
fn raw_encode_decode_matches_container() {
    let data = b"bypass the container entirely";
    let (payload, _) = encode_bytes(data, None).unwrap();
    let (back, _) = decode_bytes(&payload, DEFAULT_MAX_OUTPUT).unwrap();
    assert_eq!(back, data);
}

#[test]
fn exhaustive_short_binary_sequences() {
    // Every binary sequence of length 0..=9 (2^10 - 1 = 1023 sequences).
    for len in 0u32..=9 {
        let total = 1u64 << len;
        for bits in 0..total {
            let data: Vec<u8> = (0..len).map(|i| ((bits >> i) & 1) as u8).collect();
            let (blob, _) = pack(&data, None).unwrap();
            let (back, _) = unpack(&blob, None).unwrap();
            assert_eq!(back, data, "failed for bits={bits} len={len}");
        }
    }
}

#[test]
fn exhaustive_short_ternary_sequences() {
    // Every ternary sequence over {0, 1, 255} of length 0..=5.
    let alphabet = [0u8, 1, 255];
    for len in 0u32..=5 {
        let total = 3u64.pow(len);
        for code in 0..total {
            let mut c = code;
            let mut data = Vec::with_capacity(len as usize);
            for _ in 0..len {
                data.push(alphabet[(c % 3) as usize]);
                c /= 3;
            }
            let (blob, _) = pack(&data, None).unwrap();
            let (back, _) = unpack(&blob, None).unwrap();
            assert_eq!(back, data, "failed ternary code={code} len={len}");
        }
    }
}
