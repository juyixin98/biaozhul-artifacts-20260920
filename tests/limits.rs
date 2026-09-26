//! Resource-limit tests: total input bytes and total emitted code points
//! are bounded; hitting a limit stops the decoder loudly (never silently
//! truncates).

use inc_utf8::{ErrorKind, IncrementalDecoder, Limits, Recovery};

fn tight_limits(max_input: u64, max_output: u64) -> Limits {
    Limits {
        max_input_bytes: max_input,
        max_output_codepoints: max_output,
    }
}

#[test]
fn input_limit_stops_processing() {
    let mut dec = IncrementalDecoder::new(tight_limits(4, u64::MAX), Recovery::FailFast);
    let out = dec.feed(b"abcdefgh");
    assert!(out.stopped);
    assert_eq!(out.codepoints, vec![0x61, 0x62, 0x63, 0x64]); // abcd
    assert_eq!(out.errors.len(), 1);
    assert_eq!(out.errors[0].kind, ErrorKind::InputLimitExceeded);
    assert_eq!(out.errors[0].offset, 4);
    assert_eq!(dec.total_consumed(), 4);
}

#[test]
fn input_limit_spans_multiple_feeds() {
    let mut dec = IncrementalDecoder::new(tight_limits(5, u64::MAX), Recovery::FailFast);
    let out1 = dec.feed(b"abc");
    assert!(!out1.stopped && out1.errors.is_empty());
    let out2 = dec.feed(b"defgh");
    assert!(out2.stopped);
    assert_eq!(out2.codepoints, vec![0x64, 0x65]); // d, e
    assert_eq!(out2.errors[0].kind, ErrorKind::InputLimitExceeded);
    assert_eq!(dec.total_consumed(), 5);
}

#[test]
fn output_limit_stops_processing() {
    // 4-byte input but only 2 code points may be emitted.
    let mut dec = IncrementalDecoder::new(tight_limits(u64::MAX, 2), Recovery::FailFast);
    let out = dec.feed("a中b🦀".as_bytes());
    assert!(out.stopped);
    assert_eq!(out.codepoints, vec![0x61, 0x4E2D]); // a, 中
    assert_eq!(out.errors[0].kind, ErrorKind::OutputLimitExceeded);
}

#[test]
fn limits_enforced_even_in_skip_mode() {
    // Recovery policy must not weaken resource limits.
    let mut dec = IncrementalDecoder::new(tight_limits(3, u64::MAX), Recovery::SkipInvalidBytes);
    let out = dec.feed(b"abcdef");
    assert!(out.stopped);
    assert_eq!(out.errors[0].kind, ErrorKind::InputLimitExceeded);
}

#[test]
fn unlimited_allows_large_streams() {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let data = vec![0x61u8; 1_000_000];
    let out = dec.feed(&data);
    assert!(out.errors.is_empty());
    assert_eq!(out.codepoints.len(), 1_000_000);
    assert!(dec.finish().is_none());
}

#[test]
fn default_limits_exist() {
    let l = Limits::default();
    assert!(l.max_input_bytes > 0 && l.max_input_bytes <= 1024 * 1024 * 1024);
    assert!(l.max_output_codepoints > 0);
}
