//! Roundtrip tests: encode(codepoint) -> bytes -> decode(bytes) must be
//! the identity over the entire Unicode scalar value space, and the
//! decoder must agree with the encoder at every chunk size.

use inc_utf8::{encode_all, encode_scalar, IncrementalDecoder, Limits, Recovery};

/// All Unicode scalar values: 0..=0x10FFFF minus the surrogate range.
fn all_scalar_values() -> impl Iterator<Item = u32> {
    (0u32..=0x10FFFF).filter(|cp| !(0xD800..=0xDFFF).contains(cp))
}

#[test]
fn exhaustive_encode_decode_roundtrip() {
    // Encode every scalar value, concatenate, decode in one shot, compare.
    let expected: Vec<u32> = all_scalar_values().collect();
    let bytes = encode_all(&expected).expect("all scalar values must encode");

    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let out = dec.feed(&bytes);
    assert!(
        out.errors.is_empty(),
        "errors: {:?}",
        &out.errors[..out.errors.len().min(5)]
    );
    assert!(dec.finish().is_none());
    assert_eq!(out.codepoints.len(), expected.len());
    assert_eq!(out.codepoints, expected);
}

#[test]
fn exhaustive_roundtrip_byte_at_a_time() {
    // Same corpus, fed one byte at a time: the heaviest possible
    // chunk-boundary stress for the state machine.
    let expected: Vec<u32> = all_scalar_values().collect();
    let bytes = encode_all(&expected).unwrap();

    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let mut actual = Vec::with_capacity(expected.len());
    for &b in &bytes {
        let out = dec.feed(&[b]);
        assert!(out.errors.is_empty());
        actual.extend_from_slice(&out.codepoints);
    }
    assert!(dec.finish().is_none());
    assert_eq!(actual, expected);
}

#[test]
fn encoder_rejects_non_scalar_values() {
    for cp in [0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0x110000, 0xFFFFFF] {
        assert!(encode_scalar(cp).is_err(), "encode must reject U+{cp:X}");
    }
}

#[test]
fn encoder_matches_std() {
    // Spot-check our encoder against char::encode_utf8 across the space.
    for cp in all_scalar_values().step_by(97) {
        let (buf, len) = encode_scalar(cp).unwrap();
        let mut std_buf = [0u8; 4];
        let std_enc = char::from_u32(cp).unwrap().encode_utf8(&mut std_buf);
        assert_eq!(
            &buf[..len],
            std_enc.as_bytes(),
            "encoding mismatch for U+{cp:04X}"
        );
    }
}

#[test]
fn chunked_roundtrip_various_sizes() {
    let expected: Vec<u32> = all_scalar_values().step_by(13).collect();
    let bytes = encode_all(&expected).unwrap();
    for chunk_size in [1usize, 2, 3, 4, 5, 7, 64, 4096] {
        let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
        let mut actual = Vec::new();
        for chunk in bytes.chunks(chunk_size) {
            let out = dec.feed(chunk);
            assert!(out.errors.is_empty(), "chunk size {chunk_size}");
            actual.extend_from_slice(&out.codepoints);
        }
        assert!(dec.finish().is_none(), "chunk size {chunk_size}");
        assert_eq!(actual, expected, "chunk size {chunk_size}");
    }
}
