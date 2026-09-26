//! Split-point tests: every valid input is decoded in one shot and then
//! re-decoded after splitting the byte stream at *every* possible byte
//! position (and, for a sample, at every pair of positions). The decoder
//! must produce identical code points and no errors in all cases.

use inc_utf8::{IncrementalDecoder, Limits, Recovery};

/// Decode `bytes` in fixed-size chunks; returns (codepoints, errors).
fn decode_chunked(bytes: &[u8], chunk_size: usize) -> (Vec<u32>, Vec<String>) {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let mut cps = Vec::new();
    let mut errs = Vec::new();
    for chunk in bytes.chunks(chunk_size.max(1)) {
        let out = dec.feed(chunk);
        cps.extend_from_slice(&out.codepoints);
        errs.extend(out.errors.iter().map(|e| e.to_string()));
    }
    if let Some(e) = dec.finish() {
        errs.push(e.to_string());
    }
    (cps, errs)
}

/// Sample texts covering 1-, 2-, 3- and 4-byte sequences, including
/// boundary code points (U+007F/U+0080, U+07FF/U+0800, U+FFFF/U+10000,
/// U+10FFFF) and mixed content.
fn sample_inputs() -> Vec<Vec<u8>> {
    let texts = [
        "",
        "a",
        "hello world",
        "\u{7F}\u{80}",
        "\u{7FF}\u{800}",
        "\u{FFFF}\u{10000}",
        "\u{10FFFF}",
        "中文测试",
        "héllo, 世界! 🦀 rust",
        "🦀🦀🦀",
        "a\u{80}b\u{800}c\u{10000}d",
        "混合 mixed \u{10FFFF} content \u{0}\u{7F}",
    ];
    texts.iter().map(|t| t.as_bytes().to_vec()).collect()
}

#[test]
fn one_shot_matches_std() {
    for bytes in sample_inputs() {
        let (cps, errs) = decode_chunked(&bytes, usize::MAX);
        assert!(errs.is_empty(), "unexpected errors for {bytes:?}: {errs:?}");
        let expected: Vec<u32> = std::str::from_utf8(&bytes)
            .unwrap()
            .chars()
            .map(|c| c as u32)
            .collect();
        assert_eq!(cps, expected, "one-shot decode mismatch for {bytes:?}");
    }
}

#[test]
fn every_two_chunk_split_point() {
    for bytes in sample_inputs() {
        let (expected, errs) = decode_chunked(&bytes, usize::MAX);
        assert!(errs.is_empty());
        for split in 0..=bytes.len() {
            let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
            let first = dec.feed(&bytes[..split]);
            let second = dec.feed(&bytes[split..]);
            let mut cps = first.codepoints;
            cps.extend(second.codepoints);
            let finish_err = dec.finish();
            assert!(
                first.errors.is_empty() && second.errors.is_empty() && finish_err.is_none(),
                "split at {split} of {bytes:?} produced errors: {:?} {:?} {:?}",
                first.errors,
                second.errors,
                finish_err
            );
            assert_eq!(cps, expected, "split at {split} of {bytes:?} mismatch");
        }
    }
}

#[test]
fn every_three_chunk_split_point_pair() {
    // All pairs of split positions for the shorter inputs.
    for bytes in sample_inputs().into_iter().filter(|b| b.len() <= 12) {
        let (expected, _) = decode_chunked(&bytes, usize::MAX);
        for i in 0..=bytes.len() {
            for j in i..=bytes.len() {
                let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
                let mut cps = Vec::new();
                let mut errors = 0;
                for chunk in [&bytes[..i], &bytes[i..j], &bytes[j..]] {
                    let out = dec.feed(chunk);
                    cps.extend_from_slice(&out.codepoints);
                    errors += out.errors.len();
                }
                if dec.finish().is_some() {
                    errors += 1;
                }
                assert_eq!(errors, 0, "splits ({i}, {j}) of {bytes:?} produced errors");
                assert_eq!(cps, expected, "splits ({i}, {j}) of {bytes:?} mismatch");
            }
        }
    }
}

#[test]
fn byte_at_a_time_feeding() {
    for bytes in sample_inputs() {
        let (expected, _) = decode_chunked(&bytes, usize::MAX);
        let (cps, errs) = decode_chunked(&bytes, 1);
        assert!(
            errs.is_empty(),
            "byte-at-a-time errors for {bytes:?}: {errs:?}"
        );
        assert_eq!(cps, expected, "byte-at-a-time mismatch for {bytes:?}");
    }
}

#[test]
fn empty_feeds_are_noops() {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let bytes = "a中🦀".as_bytes();
    let mut cps = Vec::new();
    for &b in bytes {
        let out_empty = dec.feed(&[]);
        assert!(out_empty.codepoints.is_empty() && out_empty.errors.is_empty());
        cps.extend_from_slice(&dec.feed(&[b]).codepoints);
    }
    assert!(dec.finish().is_none());
    assert_eq!(cps, vec![0x61, 0x4E2D, 0x1F980]);
}
