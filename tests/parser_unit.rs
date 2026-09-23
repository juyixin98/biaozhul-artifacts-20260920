//! Core parser unit tests: each RESP2 type, null-vs-empty, and the limits.

use resp_incremental::{Config, ParseError, Parser, Poll, Value};

fn default() -> Parser {
    Parser::new(Config::default())
}

fn feed_all(p: &mut Parser, bytes: &[u8]) {
    // Feed in one chunk first; split tests live in split_tests.rs.
    p.feed(bytes).unwrap();
}

fn collect(p: &mut Parser) -> Vec<Value> {
    let mut out = Vec::new();
    while let Poll::Ready(v) = p.try_next() {
        out.push(v);
    }
    out
}

#[test]
fn parses_simple_string() {
    let mut p = default();
    feed_all(&mut p, b"+OK\r\n");
    assert_eq!(p.try_next(), Poll::Ready(Value::Simple("OK".into())));
    assert_eq!(p.try_next(), Poll::Pending);
    assert_eq!(p.consumed_bytes(), 5);
}

#[test]
fn parses_error_reply_including_kind() {
    let mut p = default();
    feed_all(&mut p, b"-ERR unknown command 'FOO'\r\n");
    match p.try_next() {
        Poll::Ready(Value::Error(s)) => assert_eq!(s, "ERR unknown command 'FOO'"),
        other => panic!("unexpected: {:?}", other),
    }
}

#[test]
fn parses_integers_positive_negative_and_zero() {
    let mut p = default();
    feed_all(&mut p, b":0\r\n:-1\r\n:42\r\n:-9223372036854775808\r\n:9223372036854775807\r\n");
    assert_eq!(p.try_next(), Poll::Ready(Value::Integer(0)));
    assert_eq!(p.try_next(), Poll::Ready(Value::Integer(-1)));
    assert_eq!(p.try_next(), Poll::Ready(Value::Integer(42)));
    assert_eq!(
        p.try_next(),
        Poll::Ready(Value::Integer(i64::MIN))
    );
    assert_eq!(
        p.try_next(),
        Poll::Ready(Value::Integer(i64::MAX))
    );
    assert_eq!(p.try_next(), Poll::Pending);
}

#[test]
fn parses_empty_bulk_which_is_not_null() {
    let mut p = default();
    feed_all(&mut p, b"$0\r\n\r\n");
    let v = match p.try_next() {
        Poll::Ready(v) => v,
        other => panic!("{:?}", other),
    };
    assert_eq!(v, Value::Bulk(Vec::new()));
    assert!(v.is_empty_bulk());
    assert!(!v.is_null());
}

#[test]
fn parses_null_bulk() {
    let mut p = default();
    feed_all(&mut p, b"$-1\r\n");
    let v = match p.try_next() {
        Poll::Ready(v) => v,
        other => panic!("{:?}", other),
    };
    assert_eq!(v, Value::Null);
    assert!(v.is_null());
    assert!(!v.is_empty_bulk());
}

#[test]
fn null_array_decodes_to_null() {
    let mut p = default();
    feed_all(&mut p, b"*-1\r\n");
    assert_eq!(p.try_next(), Poll::Ready(Value::Null));
}

#[test]
fn empty_array() {
    let mut p = default();
    feed_all(&mut p, b"*0\r\n");
    assert_eq!(p.try_next(), Poll::Ready(Value::Array(vec![])));
}

#[test]
fn parses_binary_bulk_with_embedded_crlf_and_nul() {
    // 9 bytes of payload: a \r\n in the middle and a NUL byte.
    let raw = b"$9\r\nab\r\ncd\x00ef\r\n";
    let mut p = default();
    feed_all(&mut p, raw);
    let v = match p.try_next() {
        Poll::Ready(v) => v,
        other => panic!("{:?}", other),
    };
    assert_eq!(v, Value::Bulk(b"ab\r\ncd\x00ef".to_vec()));
    // The frame consumes header + payload + trailer = 4+9+2 = 15 bytes.
    assert_eq!(p.consumed_bytes(), 15);
}

#[test]
fn parses_nested_array() {
    // *2 [ :1, *2 [ bulk hi, $-1 ] ]
    let wire = b"*2\r\n:1\r\n*2\r\n$2\r\nhi\r\n$-1\r\n";
    let mut p = default();
    feed_all(&mut p, wire);
    let expected = Value::Array(vec![
        Value::Integer(1),
        Value::Array(vec![Value::Bulk(b"hi".to_vec()), Value::Null]),
    ]);
    assert_eq!(p.try_next(), Poll::Ready(expected));
}

#[test]
fn parses_mixed_pipeline_of_three_messages() {
    let wire = b"+OK\r\n:7\r\n$3\r\nfoo\r\n";
    let mut p = default();
    feed_all(&mut p, wire);
    let frames = collect(&mut p);
    assert_eq!(
        frames,
        vec![
            Value::Simple("OK".into()),
            Value::Integer(7),
            Value::Bulk(b"foo".to_vec())
        ]
    );
    assert_eq!(p.consumed_bytes(), wire.len());
}

#[test]
fn trailing_bytes_after_one_frame_become_the_next() {
    let mut p = default();
    feed_all(&mut p, b":1\r\n:2\r");
    assert_eq!(p.try_next(), Poll::Ready(Value::Integer(1)));
    // ":2\r" without LF: incomplete.
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"\n").unwrap();
    assert_eq!(p.try_next(), Poll::Ready(Value::Integer(2)));
}

// ---------- incremental delivery -------------------------------------------

#[test]
fn byte_by_byte_delivery_of_bulk_with_embedded_crlf() {
    let wire = b"$9\r\nab\r\ncd\x00ef\r\n";
    let mut p = default();
    for (i, &b) in wire.iter().enumerate() {
        p.feed(&[b]).unwrap();
        if i + 1 < wire.len() {
            assert_eq!(p.try_next(), Poll::Pending, "premature frame at {}", i + 1);
        }
    }
    assert_eq!(
        p.try_next(),
        Poll::Ready(Value::Bulk(b"ab\r\ncd\x00ef".to_vec()))
    );
}

#[test]
fn header_only_then_payload_in_two_chunks() {
    let mut p = default();
    p.feed(b"$5\r\nhe").unwrap();
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"ll").unwrap();
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"o\r\n").unwrap();
    assert_eq!(p.try_next(), Poll::Ready(Value::Bulk(b"hello".to_vec())));
}

#[test]
fn split_exactly_on_the_cr_lf_boundary() {
    let mut p = default();
    p.feed(b"+OK\r").unwrap();
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"\n").unwrap();
    assert_eq!(p.try_next(), Poll::Ready(Value::Simple("OK".into())));
}

// ---------- error cases -----------------------------------------------------

#[test]
fn unknown_type_byte_is_fatal_and_poisons() {
    let mut p = default();
    p.feed(b"@x\r\n").unwrap();
    match p.try_next() {
        Poll::Error(ParseError::UnknownTypeByte(b'@')) => {}
        other => panic!("{:?}", other),
    }
    assert!(p.is_poisoned());
    assert_eq!(p.feed(b"+OK\r\n"), Err(ParseError::ParserPoisoned));
    assert_eq!(p.try_next(), Poll::Error(ParseError::ParserPoisoned));
}

#[test]
fn bare_lf_rejected() {
    let mut p = default();
    p.feed(b"+OK\n").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::InvalidLineEnding { byte: b'\n' })
    ));
}

#[test]
fn cr_followed_by_non_lf_rejected() {
    let mut p = default();
    p.feed(b"+OK\rX").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::InvalidLineEnding { byte: b'X' })
    ));
}

#[test]
fn invalid_integer_fields() {
    for (wire, field) in [
        (b":abc\r\n".as_slice(), "integer"),
        (b":12x\r\n".as_slice(), "integer"),
        (b":-\r\n".as_slice(), "integer"),
        (b":+1\r\n".as_slice(), "integer"),
        (b"$xx\r\n".as_slice(), "bulk length"),
        (b"$--\r\n".as_slice(), "bulk length"),
    ] {
        let mut p = default();
        p.feed(wire).unwrap();
        match p.try_next() {
            Poll::Error(ParseError::InvalidInteger { field: f, .. }) => assert_eq!(f, field),
            other => panic!("{:?} for {:?}", other, wire),
        }
    }
}

#[test]
fn integer_overflow_rejected() {
    let mut p = default();
    p.feed(b":9223372036854775808\r\n").unwrap(); // i64::MAX + 1
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::IntegerOverflow(_))
    ));

    let mut p = default();
    p.feed(b":-9223372036854775809\r\n").unwrap(); // i64::MIN - 1
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::IntegerOverflow(_))
    ));
}

#[test]
fn negative_bulk_length_other_than_minus_one_is_illegal() {
    for wire in [b"$-2\r\n".as_slice(), b"$-100\r\n", b"$-9999999999\r\n"] {
        let mut p = default();
        p.feed(wire).unwrap();
        match p.try_next() {
            Poll::Error(ParseError::InvalidBulkLength(n)) => assert!(n < -1),
            other => panic!("{:?} for {:?}", other, wire),
        }
        assert!(p.is_poisoned());
    }
}

#[test]
fn declared_bulk_payload_missing_trailer_is_incomplete_not_error() {
    let mut p = default();
    p.feed(b"$3\r\nabc").unwrap(); // no CRLF yet
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"\r").unwrap();
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"\n").unwrap();
    assert_eq!(p.try_next(), Poll::Ready(Value::Bulk(b"abc".to_vec())));
}

#[test]
fn bulk_trailer_must_be_crlf() {
    // Payload of length 3 followed by bogus trailer — the 4th payload byte is
    // NOT part of the payload, so it must be \r.
    let mut p = default();
    p.feed(b"$3\r\nabcXX").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::InvalidLineEnding { byte: b'X' })
    ));
}

#[test]
fn non_utf8_simple_string_is_rejected_but_bulk_keeps_bytes() {
    let mut p = default();
    p.feed(b"+\xff\xfe\r\n").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::InvalidUtf8 { .. })
    ));

    let mut p = default();
    p.feed(b"$2\r\n\xff\xfe\r\n").unwrap();
    assert_eq!(
        p.try_next(),
        Poll::Ready(Value::Bulk(vec![0xff, 0xfe]))
    );
}

// ---------- configurable limits --------------------------------------------

#[test]
fn max_bulk_length_enforced() {
    let cfg = Config {
        max_bulk_length: 4,
        ..Config::default()
    };
    let mut p = Parser::new(cfg);
    p.feed(b"$5\r\nhello\r\n").unwrap();
    assert_eq!(
        p.try_next(),
        Poll::Error(ParseError::BulkTooLarge {
            declared: 5,
            max: 4
        })
    );
}

#[test]
fn max_array_length_enforced() {
    let cfg = Config {
        max_array_length: 1,
        ..Config::default()
    };
    let mut p = Parser::new(cfg);
    p.feed(b"*2\r\n:1\r\n:2\r\n").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::ArrayTooLarge {
            declared: 2,
            max: 1
        })
    ));
}

#[test]
fn nesting_depth_enforced_at_and_past_limit() {
    // max_depth = 2 => legal: [[[]]] (outer=0, then 1, 2).
    let legal = b"*1\r\n*1\r\n*1\r\n:1\r\n";
    let cfg = Config {
        max_depth: 2,
        ..Config::default()
    };
    let mut p = Parser::new(cfg.clone());
    p.feed(legal).unwrap();
    assert!(p.try_next().is_ready());

    // One more level: [[[[]]]]
    let too_deep = b"*1\r\n*1\r\n*1\r\n*1\r\n:1\r\n";
    let mut p = Parser::new(cfg);
    p.feed(too_deep).unwrap();
    assert_eq!(
        p.try_next(),
        Poll::Error(ParseError::NestingTooDeep { depth: 3, max: 2 })
    );
}

#[test]
fn total_byte_budget_counts_only_completed_frames() {
    let cfg = Config {
        max_total_bytes: 10,
        ..Config::default()
    };
    let mut p = Parser::new(cfg);
    p.feed(b":1234\r\n").unwrap(); // 7 bytes
    assert!(matches!(p.try_next(), Poll::Ready(_)));
    assert_eq!(p.consumed_bytes(), 7);

    // A second 7-byte frame would exceed 10.
    p.feed(b":5678\r\n").unwrap();
    assert_eq!(
        p.try_next(),
        Poll::Error(ParseError::BudgetExceeded {
            needed: 7,
            remaining: 3
        })
    );

    // But while the second frame is incomplete, no budget is consumed.
    let mut p = Parser::new(Config {
        max_total_bytes: 10,
        ..Config::default()
    });
    p.feed(b":1234\r\n").unwrap();
    let _ = p.try_next();
    p.feed(b":99").unwrap(); // 3 bytes, incomplete
    assert_eq!(p.try_next(), Poll::Pending);
    assert_eq!(p.consumed_bytes(), 7);
}

#[test]
fn oversized_bulk_declared_against_remaining_budget_fails_before_payload_arrives() {
    let cfg = Config {
        max_bulk_length: 10_000,
        max_total_bytes: 8,
        ..Config::default()
    };
    let mut p = Parser::new(cfg);
    // Only the header has been fed; $100 can never fit in 8 bytes total.
    p.feed(b"$100\r\n").unwrap();
    // The 6-byte header is charged first; the remaining 2 bytes can never
    // cover the 102 bytes (100 payload + CRLF) the declared frame needs.
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::BudgetExceeded {
            needed: 102,
            remaining: 2
        })
    ));
}

#[test]
fn line_too_long_when_crlf_never_arrives() {
    let cfg = Config {
        max_line_length: 3,
        ..Config::default()
    };
    let mut p = Parser::new(cfg);
    p.feed(b"+hello").unwrap();
    assert!(matches!(
        p.try_next(),
        Poll::Error(ParseError::LineTooLong { length: 5, max: 3 })
    ));
}

#[test]
fn empty_feed_and_empty_buffer_are_pending() {
    let mut p = default();
    assert_eq!(p.try_next(), Poll::Pending);
    p.feed(b"").unwrap();
    assert_eq!(p.try_next(), Poll::Pending);
}

trait IsReady {
    fn is_ready(&self) -> bool;
}
impl IsReady for Poll<Value> {
    fn is_ready(&self) -> bool {
        matches!(self, Poll::Ready(_))
    }
}
