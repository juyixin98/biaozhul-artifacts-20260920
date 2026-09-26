//! Corrupt-input tests: invalid distances, truncated tokens, bad headers,
//! and the output budget (decompression-bomb protection).

use lzsw::error::Error;
use lzsw::{compress, decompress, Decoder};

fn header() -> Vec<u8> {
    let mut h = Vec::new();
    h.extend_from_slice(b"LZSW");
    h.extend_from_slice(&[1, 0, 15, 0]);
    h
}

#[test]
fn distance_reaching_before_output_start_is_rejected() {
    let mut s = header();
    // Two literals, then a match with distance 5 > 2 bytes produced.
    s.push(1); // literal run of 2
    s.extend_from_slice(b"ab");
    s.push(0x80); // match, len 3
    s.extend_from_slice(&5u16.to_be_bytes());
    assert_eq!(
        decompress(&s, 1 << 20),
        Err(Error::DistanceTooLarge {
            distance: 5,
            produced: 2
        })
    );
}

#[test]
fn distance_at_first_byte_is_rejected() {
    let mut s = header();
    s.push(0x80); // match with no prior output at all
    s.extend_from_slice(&1u16.to_be_bytes());
    assert!(matches!(
        decompress(&s, 1 << 20),
        Err(Error::DistanceTooLarge { .. })
    ));
}

#[test]
fn distance_beyond_window_is_rejected() {
    let mut s = header();
    s.push(0x80);
    s.extend_from_slice(&40_000u16.to_be_bytes()); // > 32768
    assert_eq!(
        decompress(&s, 1 << 20),
        Err(Error::DistanceExceedsWindow { distance: 40_000 })
    );
}

#[test]
fn max_valid_distance_is_accepted() {
    // Produce 32768 literal bytes, then a match at distance 32768.
    let data: Vec<u8> = (0..=255u8).cycle().take(32768).collect();
    let mut s = header();
    for chunk in data.chunks(128) {
        s.push((chunk.len() - 1) as u8);
        s.extend_from_slice(chunk);
    }
    s.push(0x80); // len 3
    s.extend_from_slice(&32_768u16.to_be_bytes());
    let out = decompress(&s, 1 << 20).unwrap();
    assert_eq!(out.len(), 32768 + 3);
    assert_eq!(&out[32768..], &data[..3]);
}

#[test]
fn overlapping_copy_repeats_pattern() {
    let mut s = header();
    s.push(0); // one literal
    s.push(b'x');
    s.push(0x80 | 9); // len 12, distance 1 -> 12 more 'x'
    s.extend_from_slice(&1u16.to_be_bytes());
    assert_eq!(decompress(&s, 1 << 20).unwrap(), vec![b'x'; 13]);
}

#[test]
fn truncated_literal_run_is_rejected() {
    let mut s = header();
    s.push(4); // announces 5 literals
    s.extend_from_slice(b"ab"); // but only 2 follow
    assert_eq!(decompress(&s, 1 << 20), Err(Error::TruncatedToken));
}

#[test]
fn truncated_match_token_is_rejected() {
    let mut s = header();
    s.push(0x80); // match tag, but the 2 distance bytes never arrive
    assert_eq!(decompress(&s, 1 << 20), Err(Error::TruncatedToken));

    let mut s2 = header();
    s2.push(0x80);
    s2.push(0x00); // only one of two distance bytes
    assert_eq!(decompress(&s2, 1 << 20), Err(Error::TruncatedToken));
}

#[test]
fn truncated_header_is_rejected() {
    assert_eq!(decompress(b"LZ", 1 << 20), Err(Error::TruncatedToken));
    assert_eq!(decompress(b"", 1 << 20), Err(Error::TruncatedToken));
}

#[test]
fn bad_magic_is_rejected() {
    let mut s = header();
    s[0] = b'X';
    assert_eq!(decompress(&s, 1 << 20), Err(Error::InvalidMagic));
}

#[test]
fn bad_version_is_rejected() {
    let mut s = header();
    s[4] = 99;
    assert_eq!(decompress(&s, 1 << 20), Err(Error::UnsupportedVersion(99)));
}

#[test]
fn output_budget_stops_bomb() {
    // 100k of 'a' compresses to ~2.4 KB; a tiny budget must stop it.
    let compressed = compress(&vec![b'a'; 100_000]);
    assert!(compressed.len() < 4096);
    assert_eq!(
        decompress(&compressed, 100),
        Err(Error::OutputBudgetExceeded {
            limit: 100,
            attempted: 131 // 1 literal + first match token asks for 130 more
        })
    );
}

#[test]
fn exact_budget_is_allowed() {
    let data = b"hello world, hello world, hello!";
    let compressed = compress(data);
    let out = decompress(&compressed, data.len() as u64).unwrap();
    assert_eq!(out, data);
    // One byte less must fail.
    assert!(matches!(
        decompress(&compressed, data.len() as u64 - 1),
        Err(Error::OutputBudgetExceeded { .. })
    ));
}

#[test]
fn update_after_finish_is_rejected() {
    let compressed = compress(b"abc");
    let mut dec = Decoder::new(1 << 20);
    dec.update(&compressed).unwrap();
    dec.finish().unwrap();
    assert_eq!(dec.update(b"x"), Err(Error::AlreadyFinished));
    assert_eq!(dec.finish(), Err(Error::AlreadyFinished));
}
