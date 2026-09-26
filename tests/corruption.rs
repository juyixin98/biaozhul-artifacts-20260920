//! Corruption and adversarial-input tests: truncated bitstreams, forged
//! code-length tables (oversubscribed / incomplete / invalid), forged
//! declared lengths, trailing garbage, and output limits.

use canohuff::decode::{decompress_slice, DecodeOptions, IncompletePolicy};
use canohuff::encode::compress_slice;
use canohuff::Error;

/// Build a raw stream: header with the given table entries, then `payload`.
fn stream(original_len: u64, entries: &[(u8, u8)], payload: &[u8]) -> Vec<u8> {
    let mut v = b"CHF1".to_vec();
    v.extend_from_slice(&original_len.to_le_bytes());
    v.extend_from_slice(&(entries.len() as u16).to_le_bytes());
    for &(s, l) in entries {
        v.push(s);
        v.push(l);
    }
    v.extend_from_slice(payload);
    v
}

fn default_opts() -> DecodeOptions {
    DecodeOptions::default()
}

#[test]
fn truncated_header_is_rejected() {
    let compressed = compress_slice(b"hello world, hello huffman").unwrap();
    for cut in 0..14usize {
        let err = decompress_slice(&compressed[..cut], &default_opts()).unwrap_err();
        assert!(
            matches!(err, Error::Truncated),
            "cut at {cut}: expected Truncated, got {err}"
        );
    }
}

#[test]
fn truncated_bitstream_is_rejected() {
    let data: Vec<u8> = (0..1000u32).map(|i| (i * 7 % 13) as u8).collect();
    let compressed = compress_slice(&data).unwrap();
    // Cut inside the bitstream at several points.
    for cut in [20, compressed.len() / 2, compressed.len() - 1] {
        let err = decompress_slice(&compressed[..cut], &default_opts()).unwrap_err();
        assert!(
            matches!(err, Error::Truncated),
            "cut at {cut}: expected Truncated, got {err}"
        );
    }
}

#[test]
fn bad_magic_is_rejected() {
    let mut s = stream(0, &[], &[]);
    s[0] = b'X';
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::BadMagic));
}

#[test]
fn oversubscribed_table_is_rejected() {
    // Three 1-bit codes: Kraft sum 3 * 2^-1 = 1.5 > 1.
    let s = stream(0, &[(0, 1), (1, 1), (2, 1)], &[]);
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::Oversubscribed));
}

#[test]
fn incomplete_table_policy() {
    // One symbol with a 5-bit code: Kraft sum 2^-5 << 1, incomplete.
    let s = stream(0, &[(65, 5)], &[]);
    // Default policy permits it (canonical encoders emit such tables).
    let out = decompress_slice(&s, &default_opts()).unwrap();
    assert!(out.is_empty());
    // Strict policy rejects it up front.
    let strict = DecodeOptions {
        incomplete_policy: IncompletePolicy::Reject,
        ..default_opts()
    };
    let err = decompress_slice(&s, &strict).unwrap_err();
    assert!(matches!(err, Error::IncompleteTable));
}

#[test]
fn invalid_code_lengths_are_rejected() {
    for len in [0u8, 33, 255] {
        let s = stream(0, &[(0, len)], &[]);
        let err = decompress_slice(&s, &default_opts()).unwrap_err();
        assert!(
            matches!(err, Error::InvalidCodeLength(l) if l == len),
            "len {len}: got {err}"
        );
    }
}

#[test]
fn duplicate_symbol_is_rejected() {
    let s = stream(0, &[(7, 1), (7, 2)], &[]);
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::DuplicateSymbol(7)));
}

#[test]
fn forged_declared_length_hits_output_limit() {
    // Claim 2^64-1 bytes of output: must be refused before decoding.
    let s = stream(u64::MAX, &[(0, 1)], &[0x00; 8]);
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::OutputTooLarge { .. }));
}

#[test]
fn forged_small_declared_length_stops_early() {
    // Valid stream for 1000 bytes, but header patched to declare 10:
    // decoding stops after 10 symbols and the rest is trailing data.
    let data = vec![b'a'; 1000];
    let mut compressed = compress_slice(&data).unwrap();
    compressed[4..12].copy_from_slice(&10u64.to_le_bytes());
    let err = decompress_slice(&compressed, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::TrailingData));
}

#[test]
fn trailing_garbage_is_rejected() {
    let compressed = compress_slice(b"some data to compress").unwrap();
    let mut with_extra = compressed.clone();
    with_extra.push(0x00);
    let err = decompress_slice(&with_extra, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::TrailingData));
}

#[test]
fn nonzero_padding_bits_are_rejected() {
    // Single symbol => 1-bit code, 7 padding bits in the final byte.
    let mut compressed = compress_slice(&vec![0x55; 3]).unwrap();
    let last = compressed.len() - 1;
    compressed[last] |= 0x01; // set a padding bit
    let err = decompress_slice(&compressed, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::TrailingData));
}

#[test]
fn output_limit_is_enforced() {
    let data: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
    let compressed = compress_slice(&data).unwrap();
    let opts = DecodeOptions {
        max_output_bytes: 500,
        ..default_opts()
    };
    let err = decompress_slice(&compressed, &opts).unwrap_err();
    assert!(matches!(err, Error::OutputTooLarge { needed: 1000, limit: 500 }));
}

#[test]
fn empty_table_with_data_is_rejected() {
    let s = stream(5, &[], &[0x00]);
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::EmptyTableWithData));
}

#[test]
fn undefined_code_under_permit_policy() {
    // Incomplete table: symbol 'a' has code 0 (1 bit). Feed a 1 bit,
    // which matches no code => UndefinedCode.
    let s = stream(1, &[(b'a', 1)], &[0x80]); // bitstream: 1 then padding
    let err = decompress_slice(&s, &default_opts()).unwrap_err();
    assert!(matches!(err, Error::UndefinedCode));
}
