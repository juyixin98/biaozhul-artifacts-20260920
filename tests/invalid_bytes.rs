//! Invalid-input tests, including an exhaustive sweep of all 256 single
//! bytes and all 65536 two-byte combinations against an independent
//! reference implementation written directly from the RFC 3629 table.

use incutf8::decoder::{decode_all, Decoder, ErrorPolicy};
use incutf8::error::ErrorKind;

/// What the reference expects for a short input under the abort policy.
struct Expected {
    output: Vec<u8>,
    /// (kind, offset) pairs in stream order.
    errors: Vec<(ErrorKind, u64)>,
}

/// Independent reference: classify one lead byte per the RFC 3629 table.
/// Returns (continuation count, min first cont, max first cont, violation kind).
fn lead_class(b: u8) -> Option<(u8, u8, u8, ErrorKind)> {
    match b {
        0xC2..=0xDF => Some((1, 0x80, 0xBF, ErrorKind::InvalidContinuationByte)),
        0xE0 => Some((2, 0xA0, 0xBF, ErrorKind::OverlongEncoding)),
        0xE1..=0xEC | 0xEE..=0xEF => Some((2, 0x80, 0xBF, ErrorKind::InvalidContinuationByte)),
        0xED => Some((2, 0x80, 0x9F, ErrorKind::SurrogateCodePoint)),
        0xF0 => Some((3, 0x90, 0xBF, ErrorKind::OverlongEncoding)),
        0xF1..=0xF3 => Some((3, 0x80, 0xBF, ErrorKind::InvalidContinuationByte)),
        0xF4 => Some((3, 0x80, 0x8F, ErrorKind::CodePointOutOfRange)),
        _ => None,
    }
}

/// Independent reference for a one- or two-byte input under the abort
/// policy, written directly from the spec (shares no logic with the
/// decoder under test).
fn reference(input: &[u8]) -> Expected {
    let mut output = Vec::new();
    let mut errors: Vec<(ErrorKind, u64)> = Vec::new();
    let mut i = 0usize;
    while i < input.len() {
        let b0 = input[i];
        let offset = i as u64;
        if b0 <= 0x7F {
            output.push(b0);
            i += 1;
            continue;
        }
        let ground_kind = match b0 {
            0x80..=0xBF => Some(ErrorKind::InvalidLeadByte),
            0xC0..=0xC1 => Some(ErrorKind::OverlongEncoding),
            0xF5..=0xF7 => Some(ErrorKind::CodePointOutOfRange),
            0xF8..=0xFF => Some(ErrorKind::InvalidLeadByte),
            _ => None,
        };
        if let Some(kind) = ground_kind {
            errors.push((kind, offset));
            break; // abort policy
        }
        let (needed, mut lo, mut hi, mut violation) = lead_class(b0).unwrap();
        let mut consumed = 1usize;
        let mut failed = false;
        while consumed <= needed as usize {
            if i + consumed >= input.len() {
                // Ran out of input mid-sequence: finish() reports it.
                errors.push((ErrorKind::IncompleteSequence, offset));
                failed = true;
                break;
            }
            let b = input[i + consumed];
            if b < lo || b > hi {
                let kind = if (0x80..=0xBF).contains(&b) {
                    violation
                } else {
                    ErrorKind::InvalidContinuationByte
                };
                errors.push((kind, offset + consumed as u64));
                failed = true;
                break;
            }
            lo = 0x80;
            hi = 0xBF;
            violation = ErrorKind::InvalidContinuationByte;
            consumed += 1;
        }
        if failed {
            break; // abort policy
        }
        // Complete sequence: re-encode the code point from the bytes.
        let mut cp: u32 = match needed {
            1 => u32::from(b0 & 0x1F),
            2 => u32::from(b0 & 0x0F),
            _ => u32::from(b0 & 0x07),
        };
        for k in 1..=needed as usize {
            cp = (cp << 6) | u32::from(input[i + k] & 0x3F);
        }
        let mut enc = Vec::new();
        incutf8::encode_codepoint(cp, &mut enc).expect("reference produced a scalar value");
        output.extend_from_slice(&enc);
        i += consumed;
    }
    Expected { output, errors }
}

#[test]
fn all_single_bytes() {
    for b in 0u16..=0xFF {
        let input = [b as u8];
        let expected = reference(&input);
        let report = decode_all(&input, ErrorPolicy::Abort, 1024);
        let got: Vec<(ErrorKind, u64)> =
            report.errors.iter().map(|e| (e.kind, e.offset)).collect();
        assert_eq!(got, expected.errors, "byte 0x{:02X} errors", b);
        assert_eq!(report.output, expected.output, "byte 0x{:02X} output", b);
    }
}

#[test]
fn all_two_byte_combinations() {
    for b0 in 0u16..=0xFF {
        for b1 in 0u16..=0xFF {
            let input = [b0 as u8, b1 as u8];
            let expected = reference(&input);
            let report = decode_all(&input, ErrorPolicy::Abort, 1024);
            let got: Vec<(ErrorKind, u64)> =
                report.errors.iter().map(|e| (e.kind, e.offset)).collect();
            assert_eq!(
                got, expected.errors,
                "input {:02X} {:02X} errors",
                b0, b1
            );
            assert_eq!(
                report.output, expected.output,
                "input {:02X} {:02X} output",
                b0, b1
            );
        }
    }
}

#[test]
fn specific_invalid_sequences() {
    let cases: &[(&[u8], ErrorKind, u64)] = &[
        (&[0x80], ErrorKind::InvalidLeadByte, 0),          // stray continuation
        (&[0xBF], ErrorKind::InvalidLeadByte, 0),
        (&[0xC0, 0x80], ErrorKind::OverlongEncoding, 0),   // overlong NUL
        (&[0xC1, 0xBF], ErrorKind::OverlongEncoding, 0),   // overlong '/'
        (&[0xE0, 0x80, 0x80], ErrorKind::OverlongEncoding, 1), // overlong 3-byte
        (&[0xE0, 0x9F, 0xBF], ErrorKind::OverlongEncoding, 1),
        (&[0xF0, 0x80, 0x80, 0x80], ErrorKind::OverlongEncoding, 1), // overlong 4-byte
        (&[0xF0, 0x8F, 0xBF, 0xBF], ErrorKind::OverlongEncoding, 1),
        (&[0xED, 0xA0, 0x80], ErrorKind::SurrogateCodePoint, 1),     // U+D800
        (&[0xED, 0xBF, 0xBF], ErrorKind::SurrogateCodePoint, 1),     // U+DFFF
        (&[0xF4, 0x90, 0x80, 0x80], ErrorKind::CodePointOutOfRange, 1), // U+110000
        (&[0xF5, 0x80, 0x80, 0x80], ErrorKind::CodePointOutOfRange, 0),
        (&[0xF7, 0xBF, 0xBF, 0xBF], ErrorKind::CodePointOutOfRange, 0),
        (&[0xF8], ErrorKind::InvalidLeadByte, 0),
        (&[0xFE], ErrorKind::InvalidLeadByte, 0),
        (&[0xFF], ErrorKind::InvalidLeadByte, 0),
        (&[0xE2, 0x41], ErrorKind::InvalidContinuationByte, 1), // 'A' where cont expected
        (&[0xE2, 0x82, 0x41], ErrorKind::InvalidContinuationByte, 2),
        (&[0xF0, 0x9F, 0x41], ErrorKind::InvalidContinuationByte, 2),
    ];
    for (input, kind, offset) in cases {
        let report = decode_all(input, ErrorPolicy::Abort, 1024);
        assert_eq!(report.errors.len(), 1, "input {:02X?}", input);
        assert_eq!(report.errors[0].kind, *kind, "input {:02X?}", input);
        assert_eq!(report.errors[0].offset, *offset, "input {:02X?}", input);
        assert!(report.output.is_empty(), "input {:02X?}", input);
    }
}

#[test]
fn truncated_sequence_reports_lead_offset() {
    // "abc" (3 bytes) then an unfinished 3-byte sequence E2 82.
    let input = [0x61, 0x62, 0x63, 0xE2, 0x82];
    let report = decode_all(&input, ErrorPolicy::Abort, 1024);
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.errors[0].kind, ErrorKind::IncompleteSequence);
    assert_eq!(report.errors[0].offset, 3, "offset must be the lead byte");
    assert_eq!(report.errors[0].sequence_start, 3);
    assert_eq!(report.errors[0].sequence_len, 2);
    assert_eq!(report.output, b"abc");
}

#[test]
fn truncated_sequence_across_chunks_reports_absolute_offset() {
    let mut d = Decoder::new(ErrorPolicy::Abort);
    d.feed(b"hello ");       // 6 bytes
    d.feed(&[0xF0, 0x9F]);   // start of a 4-byte sequence, never finished
    let report = d.finish();
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.errors[0].kind, ErrorKind::IncompleteSequence);
    assert_eq!(report.errors[0].offset, 6);
    assert_eq!(report.errors[0].sequence_len, 2);
    assert_eq!(report.output, b"hello ");
}

#[test]
fn abort_policy_stops_at_first_error() {
    let report = decode_all(&[0xFF, 0xFF, 0xFF], ErrorPolicy::Abort, 1024);
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.consumed_bytes, 1, "must stop right after the bad byte");
}

#[test]
fn collect_policy_recovers_without_silent_replacement() {
    // 'A', invalid 0xFF, 'B': output must be exactly "AB" — no U+FFFD,
    // no placeholder — and the corruption must be reported.
    let report = decode_all(b"A\xFFB", ErrorPolicy::Collect, 1024);
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.errors[0].kind, ErrorKind::InvalidLeadByte);
    assert_eq!(report.errors[0].offset, 1);
    assert_eq!(report.output, b"AB");
    assert!(!report.output.windows(3).any(|w| w == [0xEF, 0xBF, 0xBD]));
}

#[test]
fn collect_policy_resynchronises_on_lead_byte() {
    // E2 then 'A' (not a continuation): error at offset 1, 'A' recovered.
    let report = decode_all(&[0xE2, 0x41, 0x42], ErrorPolicy::Collect, 1024);
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.errors[0].kind, ErrorKind::InvalidContinuationByte);
    assert_eq!(report.errors[0].offset, 1);
    assert_eq!(report.output, b"AB");
}

#[test]
fn collect_policy_gathers_multiple_errors() {
    let input = [0x61, 0xFF, 0x62, 0xC0, 0x80, 0x63, 0x80, 0x64];
    let report = decode_all(&input, ErrorPolicy::Collect, 1024);
    let kinds: Vec<ErrorKind> = report.errors.iter().map(|e| e.kind).collect();
    assert_eq!(
        kinds,
        vec![
            ErrorKind::InvalidLeadByte,   // 0xFF at 1
            ErrorKind::OverlongEncoding,  // 0xC0 at 3
            ErrorKind::InvalidLeadByte,   // stray 0x80 at 4
            ErrorKind::InvalidLeadByte,   // stray 0x80 at 6
        ]
    );
    let offsets: Vec<u64> = report.errors.iter().map(|e| e.offset).collect();
    assert_eq!(offsets, vec![1, 3, 4, 6]);
    assert_eq!(report.output, b"abcd");
}

#[test]
fn output_limit_is_enforced() {
    let report = decode_all(b"abcd", ErrorPolicy::Abort, 2);
    assert!(report.truncated);
    assert_eq!(report.output_codepoints, 2);
    assert_eq!(report.output, b"ab");
    assert_eq!(report.errors.len(), 1);
    assert_eq!(report.errors[0].kind, ErrorKind::OutputLimitExceeded);
}

#[test]
fn output_limit_zero_emits_nothing() {
    let report = decode_all(b"a", ErrorPolicy::Abort, 0);
    assert!(report.truncated);
    assert!(report.output.is_empty());
    assert_eq!(report.errors[0].kind, ErrorKind::OutputLimitExceeded);
}

#[test]
fn validator_mode_uses_no_output_buffer() {
    let mut d = Decoder::validator(ErrorPolicy::Abort);
    d.feed(b"valid input \xE4\xB8\xAD");
    let report = d.finish();
    assert!(report.ok());
    assert!(report.output.is_empty(), "validator must not buffer output");
    assert_eq!(report.output_codepoints, 13);
}
