//! Round-trip tests: parsed value -> `Value::encode` -> parse again.

use resp_incremental::{array, bulk, simple, Config, Parser, Poll, Value};

fn encode(v: &Value) -> Vec<u8> {
    v.to_wire().expect("value must be encodable")
}

fn parse_one(wire: &[u8]) -> Value {
    let mut p = Parser::new(Config::default());
    p.feed(wire).unwrap();
    match p.try_next() {
        Poll::Ready(v) => v,
        other => panic!("not ready: {:?}", other),
    }
}

#[test]
fn scalar_round_trips() {
    for v in [
        simple("OK"),
        Value::Error("ERR nope".into()),
        Value::Integer(0),
        Value::Integer(-12345),
        Value::Integer(i64::MIN),
        bulk(b"hello".to_vec()),
        bulk(Vec::new()),
        Value::Null,
    ] {
        let wire = encode(&v);
        assert_eq!(parse_one(&wire), v, "wire={:?}", wire);
    }
}

#[test]
fn binary_bulk_round_trips_including_embedded_crlf() {
    let v = bulk(b"a\r\nb\x00\xff\r\n".to_vec());
    let wire = encode(&v);
    assert_eq!(wire, b"$8\r\na\r\nb\x00\xff\r\n\r\n".to_vec());
    assert_eq!(parse_one(&wire), v);
}

#[test]
fn nested_array_round_trip() {
    let v = array([
        Value::Integer(1),
        array([bulk(b"x"), Value::Null, array([simple("deep")])]),
    ]);
    let wire = encode(&v);
    assert_eq!(parse_one(&wire), v);
}

#[test]
fn empty_vs_null_produce_distinct_wire() {
    assert_eq!(encode(&bulk(Vec::new())), b"$0\r\n\r\n");
    assert_eq!(encode(&Value::Null), b"$-1\r\n");
    assert_ne!(
        encode(&bulk(Vec::new())),
        encode(&Value::Null),
        "empty string and null must have distinct encodings"
    );
}

#[test]
fn simple_string_with_crlf_is_not_encodable_as_simple() {
    // A simple string carrying CR/LF must not be emitted with '+'; callers
    // must use a bulk instead.
    assert!(Value::Simple("a\r\nb".into()).to_wire().is_none());
    // The same text is perfectly fine as a binary-safe bulk.
    let wire = bulk(b"a\r\nb".to_vec()).to_wire().unwrap();
    assert_eq!(parse_one(&wire), bulk(b"a\r\nb".to_vec()));
}
