//! Incremental parsing tests, including the "every split point" acceptance
//! test: for each test message, feeding it in two fragments at every
//! possible split point must yield the same value as parsing it whole.

use resp_incr::{encode, Config, ParseError, Parser, Value};

/// Parse a complete message fed in one shot; expects exactly one value.
fn parse_whole(msg: &[u8]) -> Value {
    let mut p = Parser::default();
    p.feed(msg).unwrap();
    let v = p.next().unwrap().expect("expected a complete message");
    assert_eq!(p.next().unwrap(), None, "trailing bytes after message");
    v
}

/// Feed `msg` split at `at`, then drain; expects exactly one value.
fn parse_split(msg: &[u8], at: usize) -> Value {
    let mut p = Parser::default();
    p.feed(&msg[..at]).unwrap();
    if at < msg.len() {
        // No proper prefix of a sample message is itself a complete message.
        assert_eq!(p.next().unwrap(), None, "message completed too early at split {}", at);
    }
    p.feed(&msg[at..]).unwrap();
    let v = p.next().unwrap().expect("expected a complete message");
    assert_eq!(p.next().unwrap(), None, "trailing bytes after message");
    v
}

fn sample_messages() -> Vec<&'static [u8]> {
    vec![
        b"+OK\r\n",
        b"+\r\n",                                        // empty simple string
        b"-ERR unknown command\r\n",
        b":0\r\n",
        b":-9223372036854775808\r\n",                     // i64::MIN
        b":9223372036854775807\r\n",                      // i64::MAX
        b"$0\r\n\r\n",                                    // empty bulk
        b"$-1\r\n",                                       // null bulk
        b"$5\r\nhello\r\n",
        b"$4\r\na\r\nb\r\n",                              // CRLF inside bulk payload
        b"$4\r\n\x00\x01\xff\xfe\r\n",                    // binary bulk payload
        b"*-1\r\n",                                       // null array
        b"*0\r\n",                                        // empty array
        b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n",              // typical command
        b"*3\r\n*2\r\n:1\r\n:2\r\n*1\r\n+nested\r\n$-1\r\n", // nested arrays + null
        b"*2\r\n$4\r\na\r\nb\r\n*1\r\n*-1\r\n",           // CRLF-in-bulk + nested null array
    ]
}

#[test]
fn all_split_points_match_whole_parse() {
    for msg in sample_messages() {
        let want = parse_whole(msg);
        for at in 0..=msg.len() {
            let got = parse_split(msg, at);
            assert_eq!(
                got,
                want,
                "split at {} changed the result for {:?}",
                at,
                String::from_utf8_lossy(msg)
            );
        }
    }
}

#[test]
fn byte_at_a_time_feeding() {
    for msg in sample_messages() {
        let want = parse_whole(msg);
        let mut p = Parser::default();
        let mut got = None;
        for (i, b) in msg.iter().enumerate() {
            p.feed(&[*b]).unwrap();
            if let Some(v) = p.next().unwrap() {
                assert_eq!(i, msg.len() - 1, "completed before the last byte");
                got = Some(v);
            }
        }
        assert_eq!(got.as_ref(), Some(&want));
    }
}

#[test]
fn crlf_inside_bulk_payload() {
    let v = parse_whole(b"$4\r\na\r\nb\r\n");
    assert_eq!(v, Value::BulkString(Some(b"a\r\nb".to_vec())));
}

#[test]
fn bulk_payload_not_terminated_by_crlf_is_error() {
    // Declares 3 bytes but the bytes after the payload are not CRLF.
    let mut p = Parser::default();
    p.feed(b"$3\r\nabcXX").unwrap();
    assert_eq!(p.next(), Err(ParseError::MissingCrlf));
}

#[test]
fn null_and_empty_are_distinct() {
    assert_eq!(parse_whole(b"$-1\r\n"), Value::BulkString(None));
    assert_eq!(parse_whole(b"$0\r\n\r\n"), Value::BulkString(Some(vec![])));
    assert_eq!(parse_whole(b"*-1\r\n"), Value::Array(None));
    assert_eq!(parse_whole(b"*0\r\n"), Value::Array(Some(vec![])));
    assert_eq!(parse_whole(b"+\r\n"), Value::SimpleString(String::new()));
}

#[test]
fn negative_lengths_other_than_minus_one_are_rejected() {
    for msg in [b"$-2\r\n".as_slice(), b"$-100\r\n", b"*-2\r\n", b"*-99\r\n"] {
        let mut p = Parser::default();
        p.feed(msg).unwrap();
        match p.next() {
            Err(ParseError::NegativeLength(n)) => assert!(n < -1),
            other => panic!("expected NegativeLength for {:?}, got {:?}", msg, other),
        }
    }
    // -1 itself is the null marker and must parse fine.
    assert_eq!(parse_whole(b"$-1\r\n"), Value::BulkString(None));
    assert_eq!(parse_whole(b"*-1\r\n"), Value::Array(None));
}

#[test]
fn deep_nesting_respects_configured_limit() {
    // Build [[[[...:1...]]]] with the given nesting depth.
    fn nested(depth: usize) -> Vec<u8> {
        let mut msg = b":1\r\n".to_vec();
        for _ in 0..depth {
            let mut wrapped = b"*1\r\n".to_vec();
            wrapped.extend_from_slice(&msg);
            msg = wrapped;
        }
        msg
    }

    // depth 10 message, limit 5 -> rejected. (depth counts: top array = 1)
    let msg = nested(10);
    let mut p = Parser::new(Config {
        max_depth: 5,
        ..Config::default()
    });
    p.feed(&msg).unwrap();
    assert_eq!(p.next(), Err(ParseError::DepthLimitExceeded));

    // Same message parses with the default limit.
    let v = parse_whole(&msg);
    let mut inner = &v;
    for _ in 0..10 {
        match inner {
            Value::Array(Some(items)) => inner = &items[0],
            other => panic!("expected array, got {:?}", other),
        }
    }
    assert_eq!(inner, &Value::Integer(1));

    // Limit exactly at the boundary: nesting of 5 arrays needs depth 6
    // (5 arrays + the inner integer), so max_depth 5 must still reject it.
    let msg5 = nested(5);
    let mut p = Parser::new(Config {
        max_depth: 5,
        ..Config::default()
    });
    p.feed(&msg5).unwrap();
    assert_eq!(p.next(), Err(ParseError::DepthLimitExceeded));
    let mut p = Parser::new(Config {
        max_depth: 6,
        ..Config::default()
    });
    p.feed(&msg5).unwrap();
    assert!(p.next().unwrap().is_some());
}

#[test]
fn multiple_consecutive_messages_in_one_buffer() {
    let mut p = Parser::default();
    p.feed(b"+OK\r\n:1\r\n$3\r\nfoo\r\n*-1\r\n").unwrap();
    assert_eq!(
        p.next().unwrap(),
        Some(Value::SimpleString("OK".into()))
    );
    assert_eq!(p.next().unwrap(), Some(Value::Integer(1)));
    assert_eq!(
        p.next().unwrap(),
        Some(Value::BulkString(Some(b"foo".to_vec())))
    );
    assert_eq!(p.next().unwrap(), Some(Value::Array(None)));
    assert_eq!(p.next().unwrap(), None);
    assert_eq!(p.buffered_len(), 0);
}

#[test]
fn consecutive_messages_arriving_in_fragments() {
    let stream = b"+PING\r\n*2\r\n$4\r\nECHO\r\n$2\r\nhi\r\n:99\r\n";
    let mut p = Parser::default();
    let mut values = Vec::new();
    // Feed in awkward 7-byte chunks.
    for chunk in stream.chunks(7) {
        p.feed(chunk).unwrap();
        while let Some(v) = p.next().unwrap() {
            values.push(v);
        }
    }
    assert_eq!(
        values,
        vec![
            Value::SimpleString("PING".into()),
            Value::Array(Some(vec![
                Value::BulkString(Some(b"ECHO".to_vec())),
                Value::BulkString(Some(b"hi".to_vec())),
            ])),
            Value::Integer(99),
        ]
    );
}

#[test]
fn buffer_byte_budget_is_enforced() {
    let mut p = Parser::new(Config {
        max_buffer_bytes: 16,
        ..Config::default()
    });
    // A 20-byte incomplete bulk string never fits the budget.
    assert_eq!(p.feed(b"$19\r\n0123456789"), Ok(())); // 12 bytes buffered
    assert_eq!(p.feed(b"012345678"), Err(ParseError::BufferBudgetExceeded));
}

#[test]
fn bulk_and_array_length_limits() {
    let mut p = Parser::new(Config {
        max_bulk_len: 4,
        max_array_len: 2,
        ..Config::default()
    });
    p.feed(b"$5\r\nhello\r\n").unwrap();
    assert_eq!(p.next(), Err(ParseError::BulkLengthLimitExceeded));

    p.reset();
    p.feed(b"*3\r\n:1\r\n:2\r\n:3\r\n").unwrap();
    assert_eq!(p.next(), Err(ParseError::ArrayLengthLimitExceeded));
}

#[test]
fn malformed_inputs_are_rejected() {
    let cases: Vec<(&[u8], ParseError)> = vec![
        (b"?nope\r\n", ParseError::InvalidTypeByte(b'?')),
        (b":abc\r\n", ParseError::InvalidNumber),
        (b":\r\n", ParseError::InvalidNumber),
        (b":1 2\r\n", ParseError::InvalidNumber),
        (b"$x\r\n", ParseError::InvalidNumber),
        (b"*\r\n", ParseError::InvalidNumber),
    ];
    for (msg, want) in cases {
        let mut p = Parser::default();
        p.feed(msg).unwrap();
        assert_eq!(p.next(), Err(want), "input {:?}", msg);
    }
}

#[test]
fn incomplete_input_waits_for_more_bytes() {
    let mut p = Parser::default();
    p.feed(b"$5\r\nhel").unwrap();
    assert_eq!(p.next().unwrap(), None);
    p.feed(b"lo\r").unwrap();
    assert_eq!(p.next().unwrap(), None);
    p.feed(b"\n").unwrap();
    assert_eq!(
        p.next().unwrap(),
        Some(Value::BulkString(Some(b"hello".to_vec())))
    );
}

#[test]
fn encode_round_trip() {
    for msg in sample_messages() {
        let v = parse_whole(msg);
        let encoded = encode(&v);
        assert_eq!(
            encoded, msg,
            "re-encoding differs for {:?}",
            String::from_utf8_lossy(msg)
        );
        assert_eq!(parse_whole(&encoded), v);
    }
}
