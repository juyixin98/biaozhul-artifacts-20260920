//! Split-point tests: a valid UTF-8 stream must decode identically no
//! matter where chunk boundaries fall, including inside multi-byte
//! sequences.

use incutf8::{encode_str, Decoder, ErrorPolicy};

fn corpus() -> Vec<String> {
    vec![
        String::new(),
        "a".to_string(),
        "hello, world".to_string(),
        "é".to_string(),
        "中文字符串".to_string(),
        "🦀🎉".to_string(),
        "mixed aé中🦀b tail".to_string(),
        // Boundary scalar values.
        "\u{0}\u{7F}\u{80}\u{7FF}\u{800}\u{D7FF}\u{E000}\u{FFFF}\u{10000}\u{10FFFF}".to_string(),
        // Longer mixed text.
        "The quick brown fox jumps over the lazy dog. 敏捷的棕色狐狸。🦀🦀🦀 ünïcödé"
            .repeat(8),
    ]
}

#[test]
fn every_two_chunk_split_point() {
    for s in corpus() {
        let bytes = encode_str(&s);
        for split in 0..=bytes.len() {
            let mut d = Decoder::new(ErrorPolicy::Abort);
            d.feed(&bytes[..split]);
            d.feed(&bytes[split..]);
            let report = d.finish();
            assert!(
                report.ok(),
                "split {} of {:?}: unexpected errors {:?}",
                split, s, report.errors
            );
            assert_eq!(report.output, bytes, "split {} of {:?}", split, s);
        }
    }
}

#[test]
fn byte_at_a_time() {
    for s in corpus() {
        let bytes = encode_str(&s);
        let mut d = Decoder::new(ErrorPolicy::Abort);
        for b in bytes.iter().copied() {
            d.feed(&[b]);
        }
        let report = d.finish();
        assert!(report.ok(), "byte-at-a-time {:?}: {:?}", s, report.errors);
        assert_eq!(report.output, bytes, "byte-at-a-time {:?}", s);
    }
}

#[test]
fn every_three_chunk_split_pair_on_multibyte_text() {
    // All pairs of split points for a text made only of multi-byte chars.
    let s = "é中🦀\u{80}\u{7FF}\u{800}\u{FFFF}\u{10000}\u{10FFFF}";
    let bytes = encode_str(s);
    for i in 0..=bytes.len() {
        for j in i..=bytes.len() {
            let mut d = Decoder::new(ErrorPolicy::Abort);
            d.feed(&bytes[..i]);
            d.feed(&bytes[i..j]);
            d.feed(&bytes[j..]);
            let report = d.finish();
            assert!(report.ok(), "splits ({}, {}): {:?}", i, j, report.errors);
            assert_eq!(report.output, bytes, "splits ({}, {})", i, j);
        }
    }
}

#[test]
fn pseudo_random_chunkings() {
    let s = corpus().into_iter().last().unwrap();
    let bytes = encode_str(&s);
    // Simple deterministic LCG so the test is reproducible.
    let mut state: u64 = 0x1234_5678_9ABC_DEF0;
    let mut next = move || {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (state >> 33) as usize
    };
    for _ in 0..200 {
        let mut d = Decoder::new(ErrorPolicy::Abort);
        let mut pos = 0;
        while pos < bytes.len() {
            let len = 1 + next() % 7;
            let end = (pos + len).min(bytes.len());
            d.feed(&bytes[pos..end]);
            pos = end;
        }
        let report = d.finish();
        assert!(report.ok(), "random chunking: {:?}", report.errors);
        assert_eq!(report.output, bytes, "random chunking");
    }
}

#[test]
fn pending_sequence_bytes_reported_between_chunks() {
    let bytes = encode_str("🦀"); // 4 bytes: F0 9F A6 80
    let mut d = Decoder::new(ErrorPolicy::Abort);
    d.feed(&bytes[..1]);
    assert_eq!(d.pending_sequence_bytes(), 1);
    d.feed(&bytes[1..3]);
    assert_eq!(d.pending_sequence_bytes(), 3);
    d.feed(&bytes[3..]);
    assert_eq!(d.pending_sequence_bytes(), 0);
    let report = d.finish();
    assert!(report.ok());
    assert_eq!(report.output, bytes);
}
