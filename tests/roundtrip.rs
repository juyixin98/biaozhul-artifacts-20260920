//! Roundtrip tests: encode -> decode must be the identity for every
//! Unicode scalar value, and the encoder must reject non-scalar values.

use incutf8::decoder::{decode_all, ErrorPolicy};
use incutf8::encoder::{encode_codepoint, encode_str};

#[test]
fn exhaustive_scalar_value_roundtrip() {
    let mut whole_stream = Vec::new();
    for cp in 0u32..=0x10FFFF {
        if (0xD800..=0xDFFF).contains(&cp) {
            continue; // surrogate halves are not scalar values
        }
        let mut enc = Vec::new();
        encode_codepoint(cp, &mut enc).unwrap_or_else(|e| panic!("encode U+{:04X}: {}", cp, e));
        let report = decode_all(&enc, ErrorPolicy::Abort, 16);
        assert!(report.ok(), "decode U+{:04X}: {:?}", cp, report.errors);
        assert_eq!(report.output, enc, "roundtrip U+{:04X}", cp);
        assert_eq!(report.output_codepoints, 1, "codepoint count U+{:04X}", cp);
        whole_stream.extend_from_slice(&enc);
    }
    // The concatenation of all encodings must decode to itself.
    let report = decode_all(&whole_stream, ErrorPolicy::Abort, u64::MAX);
    assert!(report.ok(), "whole stream: {:?}", report.errors);
    assert_eq!(report.output, whole_stream);
    assert_eq!(report.output_codepoints, 0x10FFFF + 1 - 0x800);
}

#[test]
fn encoder_rejects_surrogates() {
    for cp in 0xD800..=0xDFFF {
        let mut out = Vec::new();
        let err = encode_codepoint(cp, &mut out).expect_err("surrogate must be rejected");
        assert_eq!(err.kind, incutf8::EncodeErrorKind::SurrogateCodePoint);
        assert!(out.is_empty(), "failed encode must not append bytes");
    }
}

#[test]
fn encoder_rejects_out_of_range() {
    for cp in [0x11_0000u32, 0x1F_FFFF, 0xFFFF_FFFF] {
        let mut out = Vec::new();
        let err = encode_codepoint(cp, &mut out).expect_err("out of range must be rejected");
        assert_eq!(err.kind, incutf8::EncodeErrorKind::CodePointOutOfRange);
        assert!(out.is_empty());
    }
}

#[test]
fn encode_str_matches_std_utf8() {
    // Our encoder must agree with the language's own UTF-8 representation.
    let samples = [
        "",
        "ascii only",
        "héllo wörld",
        "中文字符串，标点符号。",
        "🦀🎉🚀 emoji",
        "\u{0}\u{7F}\u{80}\u{7FF}\u{800}\u{D7FF}\u{E000}\u{FFFF}\u{10000}\u{10FFFF}",
    ];
    for s in samples {
        assert_eq!(encode_str(s), s.as_bytes(), "encode_str {:?}", s);
    }
}

#[test]
fn encode_decode_text_roundtrip() {
    let samples = [
        "The quick brown fox. 敏捷的棕色狐狸。🦀 ünïcödé",
        "\u{10FFFF}\u{10000}\u{FFFF}\u{800}\u{80}\u{7F}\u{0}",
    ];
    for s in samples {
        let bytes = encode_str(s);
        let report = decode_all(&bytes, ErrorPolicy::Abort, 1_000_000);
        assert!(report.ok());
        assert_eq!(String::from_utf8(report.output).unwrap(), s);
    }
}
