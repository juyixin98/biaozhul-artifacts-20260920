//! Invalid-sequence tests: overlong encodings, surrogates, out-of-range
//! values, lone continuations, invalid lead bytes and truncated final
//! sequences. Each case asserts the exact error kind and the absolute
//! byte offset, and is exercised at every split point to prove the
//! state machine behaves identically across chunk boundaries.

use inc_utf8::{ErrorKind, IncrementalDecoder, Limits, Recovery};

struct Case {
    name: &'static str,
    bytes: &'static [u8],
    kind: ErrorKind,
    /// Absolute offset of the byte that triggers the error.
    offset: u64,
}

const CASES: &[Case] = &[
    // --- overlong encodings ---
    Case {
        name: "overlong NUL (C0 80)",
        bytes: &[0xC0, 0x80],
        kind: ErrorKind::OverlongEncoding,
        offset: 0,
    },
    Case {
        name: "overlong C1",
        bytes: &[0xC1, 0xBF],
        kind: ErrorKind::OverlongEncoding,
        offset: 0,
    },
    Case {
        name: "overlong 3-byte (E0 80 80)",
        bytes: &[0xE0, 0x80, 0x80],
        kind: ErrorKind::OverlongEncoding,
        offset: 1,
    },
    Case {
        name: "overlong 3-byte (E0 9F BF)",
        bytes: &[0xE0, 0x9F, 0xBF],
        kind: ErrorKind::OverlongEncoding,
        offset: 1,
    },
    Case {
        name: "overlong 4-byte (F0 80 80 80)",
        bytes: &[0xF0, 0x80, 0x80, 0x80],
        kind: ErrorKind::OverlongEncoding,
        offset: 1,
    },
    Case {
        name: "overlong 4-byte (F0 8F BF BF)",
        bytes: &[0xF0, 0x8F, 0xBF, 0xBF],
        kind: ErrorKind::OverlongEncoding,
        offset: 1,
    },
    // --- surrogates ---
    Case {
        name: "surrogate U+D800 (ED A0 80)",
        bytes: &[0xED, 0xA0, 0x80],
        kind: ErrorKind::Surrogate,
        offset: 1,
    },
    Case {
        name: "surrogate U+DFFF (ED BF BF)",
        bytes: &[0xED, 0xBF, 0xBF],
        kind: ErrorKind::Surrogate,
        offset: 1,
    },
    // --- out of range ---
    Case {
        name: "U+110000 (F4 90 80 80)",
        bytes: &[0xF4, 0x90, 0x80, 0x80],
        kind: ErrorKind::OutOfRange,
        offset: 1,
    },
    Case {
        name: "invalid lead F5",
        bytes: &[0xF5, 0x80, 0x80, 0x80],
        kind: ErrorKind::InvalidLeadByte,
        offset: 0,
    },
    Case {
        name: "invalid lead FF",
        bytes: &[0xFF],
        kind: ErrorKind::InvalidLeadByte,
        offset: 0,
    },
    Case {
        name: "invalid lead FE",
        bytes: &[0xFE],
        kind: ErrorKind::InvalidLeadByte,
        offset: 0,
    },
    // --- lone / misplaced continuation bytes ---
    Case {
        name: "lone continuation 80",
        bytes: &[0x80],
        kind: ErrorKind::UnexpectedContinuation,
        offset: 0,
    },
    Case {
        name: "lone continuation BF",
        bytes: &[0xBF],
        kind: ErrorKind::UnexpectedContinuation,
        offset: 0,
    },
    Case {
        name: "continuation after ASCII",
        bytes: &[0x41, 0x80],
        kind: ErrorKind::UnexpectedContinuation,
        offset: 1,
    },
    // --- malformed continuations inside a sequence ---
    Case {
        name: "2-byte seq, bad continuation",
        bytes: &[0xC2, 0x41],
        kind: ErrorKind::InvalidContinuation,
        offset: 1,
    },
    Case {
        name: "3-byte seq, bad 2nd byte",
        bytes: &[0xE1, 0xC0, 0x80],
        kind: ErrorKind::InvalidContinuation,
        offset: 1,
    },
    Case {
        name: "3-byte seq, bad 3rd byte",
        bytes: &[0xE1, 0x80, 0x41],
        kind: ErrorKind::InvalidContinuation,
        offset: 2,
    },
    Case {
        name: "4-byte seq, bad 4th byte",
        bytes: &[0xF1, 0x80, 0x80, 0x7F],
        kind: ErrorKind::InvalidContinuation,
        offset: 3,
    },
    // --- errors not at offset 0 ---
    Case {
        name: "valid prefix then FF",
        bytes: &[0x41, 0xE4, 0xB8, 0xAD, 0xFF],
        kind: ErrorKind::InvalidLeadByte,
        offset: 4,
    },
    Case {
        name: "valid prefix then surrogate",
        bytes: &[0x61, 0xED, 0xA0, 0x80],
        kind: ErrorKind::Surrogate,
        offset: 2,
    },
];

#[test]
fn invalid_sequences_fail_fast_one_shot() {
    for case in CASES {
        let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
        let out = dec.feed(case.bytes);
        assert!(
            out.stopped,
            "{}: decoder should stop in fail-fast mode",
            case.name
        );
        assert_eq!(
            out.errors.len(),
            1,
            "{}: expected exactly one error, got {:?}",
            case.name,
            out.errors
        );
        let err = out.errors[0];
        assert_eq!(err.kind, case.kind, "{}: wrong error kind", case.name);
        assert_eq!(err.offset, case.offset, "{}: wrong error offset", case.name);
    }
}

#[test]
fn invalid_sequences_detected_at_every_split_point() {
    for case in CASES {
        for split in 0..=case.bytes.len() {
            let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
            let first = dec.feed(&case.bytes[..split]);
            let second = if first.stopped {
                // Decoder latched after finding the error in chunk 1.
                Default::default()
            } else {
                dec.feed(&case.bytes[split..])
            };
            let errors: Vec<_> = first.errors.iter().chain(second.errors.iter()).collect();
            assert_eq!(
                errors.len(),
                1,
                "{}: split {split}: expected 1 error, got {errors:?}",
                case.name
            );
            assert_eq!(errors[0].kind, case.kind, "{}: split {split}", case.name);
            assert_eq!(
                errors[0].offset, case.offset,
                "{}: split {split}",
                case.name
            );
        }
    }
}

#[test]
fn truncated_sequences_reported_at_finish_with_offset() {
    // Every proper prefix of a valid multi-byte sequence must produce a
    // TruncatedSequence error at finish(), pointing at the sequence start.
    let valid: &[&[u8]] = &[
        &[0xC2, 0x80],             // 2-byte
        &[0xE4, 0xB8, 0xAD],       // 3-byte (中)
        &[0xF0, 0x9F, 0xA6, 0x80], // 4-byte (🦀)
    ];
    for seq in valid {
        for cut in 1..seq.len() {
            let prefix = &seq[..cut];
            // Plain prefix.
            let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
            let out = dec.feed(prefix);
            assert!(
                out.errors.is_empty(),
                "prefix {prefix:?} should not error yet"
            );
            let err = dec
                .finish()
                .expect("truncated sequence must error at finish");
            assert_eq!(err.kind, ErrorKind::TruncatedSequence);
            assert_eq!(err.offset, 0, "prefix {prefix:?}");
            assert_eq!(err.sequence_start, 0, "prefix {prefix:?}");

            // Prefix after valid content: offset must point at the
            // sequence start in the original stream.
            let mut stream = b"abc".to_vec();
            stream.extend_from_slice(prefix);
            let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
            let out = dec.feed(&stream);
            assert!(out.errors.is_empty());
            let err = dec.finish().expect("truncated tail must error at finish");
            assert_eq!(err.kind, ErrorKind::TruncatedSequence);
            assert_eq!(err.offset, 3, "stream {stream:?}");
            assert_eq!(err.sequence_start, 3, "stream {stream:?}");
        }
    }
}

#[test]
fn truncated_across_chunks() {
    // 中 = E4 B8 AD, fed one byte per chunk, stream ends after 2 bytes.
    let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
    assert!(dec.feed(&[0xE4]).errors.is_empty());
    assert!(dec.feed(&[0xB8]).errors.is_empty());
    let err = dec.finish().expect("must report truncation");
    assert_eq!(err.kind, ErrorKind::TruncatedSequence);
    assert_eq!(err.offset, 0);
}

#[test]
fn valid_boundaries_accepted() {
    // The exact boundary code points must decode cleanly.
    let cases: &[(&[u8], u32)] = &[
        (&[0x00], 0x00),
        (&[0x7F], 0x7F),
        (&[0xC2, 0x80], 0x80),
        (&[0xDF, 0xBF], 0x7FF),
        (&[0xE0, 0xA0, 0x80], 0x800),
        (&[0xED, 0x9F, 0xBF], 0xD7FF),
        (&[0xEE, 0x80, 0x80], 0xE000),
        (&[0xEF, 0xBF, 0xBF], 0xFFFF),
        (&[0xF0, 0x90, 0x80, 0x80], 0x10000),
        (&[0xF4, 0x8F, 0xBF, 0xBF], 0x10FFFF),
    ];
    for (bytes, expected) in cases {
        let mut dec = IncrementalDecoder::new(Limits::unlimited(), Recovery::FailFast);
        let out = dec.feed(bytes);
        assert!(out.errors.is_empty(), "{bytes:?}: {out:?}");
        assert!(dec.finish().is_none());
        assert_eq!(out.codepoints, vec![*expected], "{bytes:?}");
    }
}
