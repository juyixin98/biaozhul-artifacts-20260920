//! Error-recovery policy tests.
//!
//! The hard requirement: corrupted content is NEVER silently replaced.
//! In both policies every malformed sequence surfaces as a structured
//! error with exact offsets, and no U+FFFD replacement character ever
//! appears in the decoded output.

use inc_utf8::{ErrorKind, IncrementalDecoder, Limits, Recovery};

const REPLACEMENT_CHAR: u32 = 0xFFFD;

/// "A<FF>B<ED A0 80>C<中 truncated at end>" — mixed valid/corrupt stream.
fn corrupt_stream() -> Vec<u8> {
    let mut v = Vec::new();
    v.push(0x41); // 'A'
    v.push(0xFF); // invalid lead
    v.push(0x42); // 'B'
    v.extend_from_slice(&[0xED, 0xA0, 0x80]); // surrogate U+D800
    v.push(0x43); // 'C'
    v.extend_from_slice(&[0xE4, 0xB8]); // truncated 中
    v
}

#[test]
fn fail_fast_stops_at_first_error() {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let out = dec.feed(&corrupt_stream());
    assert!(out.stopped);
    assert_eq!(out.errors.len(), 1);
    assert_eq!(out.errors[0].kind, ErrorKind::InvalidLeadByte);
    assert_eq!(out.errors[0].offset, 1);
    // 'A' was emitted before the error; nothing after.
    assert_eq!(out.codepoints, vec![0x41]);
    // Decoder is latched: further feeds are refused without consuming.
    assert_eq!(dec.total_consumed(), 1);
    let out2 = dec.feed(b"more");
    assert!(out2.stopped);
    assert!(out2.codepoints.is_empty());
    assert_eq!(dec.total_consumed(), 1);
}

#[test]
fn skip_policy_recovers_and_reports_every_error() {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::SkipInvalidBytes);
    let out = dec.feed(&corrupt_stream());
    assert!(!out.stopped, "skip policy must not stop on data errors");

    // Every corrupt item reported with exact offsets. After the surrogate
    // error the out-of-range byte 0xA0 (and the following 0x80) is
    // re-examined as a lead byte, producing the standard
    // maximal-subpart-style cascade of UnexpectedContinuation errors.
    let kinds: Vec<_> = out.errors.iter().map(|e| (e.kind, e.offset)).collect();
    assert_eq!(
        kinds,
        vec![
            (ErrorKind::InvalidLeadByte, 1),        // FF
            (ErrorKind::Surrogate, 4),              // ED A0 ...: A0 out of range
            (ErrorKind::UnexpectedContinuation, 4), // A0 re-examined as lead
            (ErrorKind::UnexpectedContinuation, 5), // 80
        ]
    );
    // Valid content around the corruption is fully decoded.
    assert_eq!(out.codepoints, vec![0x41, 0x42, 0x43]); // A, B, C

    // The truncated tail is reported at finish with its offset.
    let err = dec.finish().expect("truncated tail must be reported");
    assert_eq!(err.kind, ErrorKind::TruncatedSequence);
    assert_eq!(err.offset, 7);
}

#[test]
fn recovery_never_emits_replacement_char() {
    // Feed a stream full of every kind of corruption; U+FFFD must never
    // appear in the output unless it was literally encoded in the input.
    let mut stream = Vec::new();
    stream.extend_from_slice(&[0xC0, 0x80]); // overlong
    stream.extend_from_slice(&[0xED, 0xA0, 0x80]); // surrogate
    stream.extend_from_slice(&[0xF4, 0x90, 0x80, 0x80]); // out of range
    stream.extend_from_slice(&[0x80, 0xBF]); // lone continuations
    stream.extend_from_slice(&[0xE1, 0x80]); // truncated (at finish)
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::SkipInvalidBytes);
    let out = dec.feed(&stream);
    let _ = dec.finish();
    assert!(!out.codepoints.contains(&REPLACEMENT_CHAR));
    assert!(
        out.errors.len() >= 4,
        "all corruptions must be reported: {:?}",
        out.errors
    );
}

#[test]
fn replacement_char_in_input_is_passed_through() {
    // U+FFFD is itself a valid code point; a well-formed encoding of it
    // must decode normally (it is not produced BY the decoder).
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::SkipInvalidBytes);
    let out = dec.feed(&[0xEF, 0xBF, 0xBD]);
    assert!(out.errors.is_empty());
    assert_eq!(out.codepoints, vec![REPLACEMENT_CHAR]);
}

#[test]
fn skip_policy_resynchronizes_on_current_byte() {
    // After "E1 80" the byte 'A' (0x41) is an invalid continuation; in
    // skip mode it must be re-examined as a lead byte and decoded as 'A'.
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::SkipInvalidBytes);
    let out = dec.feed(&[0xE1, 0x80, 0x41, 0x42]);
    assert_eq!(out.errors.len(), 1);
    assert_eq!(out.errors[0].kind, ErrorKind::InvalidContinuation);
    assert_eq!(out.errors[0].offset, 2);
    assert_eq!(out.errors[0].sequence_start, 0);
    assert_eq!(out.codepoints, vec![0x41, 0x42]); // 'A', 'B'
}

#[test]
fn skip_policy_error_offsets_match_fail_fast() {
    // The first error reported under skip must be identical to the error
    // reported under fail-fast for the same input.
    let stream = corrupt_stream();
    let mut ff = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let mut sk = IncrementalDecoder::new(Limits::unlimited(), Recovery::SkipInvalidBytes);
    let ff_err = ff.feed(&stream).errors[0];
    let sk_err = sk.feed(&stream).errors[0];
    assert_eq!(ff_err, sk_err);
}

#[test]
fn reset_clears_latched_decoder() {
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    let _ = dec.feed(&[0xFF]);
    dec.reset();
    let out = dec.feed(b"ok");
    assert!(out.errors.is_empty());
    assert_eq!(out.codepoints, vec![0x6F, 0x6B]);
    assert!(dec.finish().is_none());
}
