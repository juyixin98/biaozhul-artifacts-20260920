//! Tests for bounded decoding: per-value length, total output and nesting.

mod common;

use bse::codec::{decode_envelope, encode_envelope, EncodeLimit};
use bse::schema::{Cardinality, ScalarType};
use bse::value::{Field, Value};
use bse::wire::Limits;
use common::{msg, present, scalar, schema};

fn person_schema() -> bse::schema::Schema {
    schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Optional),
            ],
        )],
    )
}

fn encode_hello() -> Vec<u8> {
    let s = person_schema();
    let m = msg(vec![(1, present(1_i64)), (2, present("hello"))]);
    let mut buf = Vec::new();
    encode_envelope(&s, &m, &mut buf, EncodeLimit::default()).unwrap();
    buf
}

#[test]
fn decode_rejects_value_exceeding_max_value_len() {
    let bytes = encode_hello();
    let limits = Limits {
        max_value_len: 2, // the "hello" LEN value is 5 bytes
        max_output: 1 << 20,
        max_depth: 8,
    };
    let mut cursor = std::io::Cursor::new(bytes);
    let err = decode_envelope(&person_schema(), &mut cursor, limits).unwrap_err();
    assert!(matches!(
        err,
        bse::Error::LengthExceedsLimit { declared, limit } if declared == 5 && limit == 2
    ));
}

#[test]
fn decode_rejects_total_output_over_limit() {
    let bytes = encode_hello();
    let limits = Limits {
        max_value_len: 1 << 20,
        max_output: 3, // a handful of bytes only
        max_depth: 8,
    };
    let mut cursor = std::io::Cursor::new(bytes);
    let err = decode_envelope(&person_schema(), &mut cursor, limits).unwrap_err();
    assert!(matches!(err, bse::Error::OutputLimitExceeded { .. }));
}

#[test]
fn encode_rejects_output_over_limit() {
    let s = person_schema();
    let m = msg(vec![
        (1, present(1_i64)),
        (2, present("this string is longer than 8 bytes")),
    ]);
    let mut buf = Vec::new();
    let tiny = EncodeLimit { max_output: 8 };
    let err = encode_envelope(&s, &m, &mut buf, tiny).unwrap_err();
    assert!(matches!(err, bse::Error::OutputLimitExceeded { limit: 8 }));
}

#[test]
fn decode_rejects_nesting_deeper_than_limit() {
    // Build a message nested 5 deep, then allow only depth 2.
    let s = schema(
        "M0",
        vec![
            (
                "M0",
                vec![common::message_field(
                    "inner",
                    1,
                    "M1",
                    Cardinality::Optional,
                )],
            ),
            (
                "M1",
                vec![common::message_field(
                    "inner",
                    1,
                    "M2",
                    Cardinality::Optional,
                )],
            ),
            (
                "M2",
                vec![common::message_field(
                    "inner",
                    1,
                    "M3",
                    Cardinality::Optional,
                )],
            ),
            (
                "M3",
                vec![common::message_field(
                    "inner",
                    1,
                    "M4",
                    Cardinality::Optional,
                )],
            ),
            (
                "M4",
                vec![scalar("leaf", 1, ScalarType::Int64, Cardinality::Optional)],
            ),
        ],
    );

    let leaf = msg(vec![(1, present(9_i64))]);
    let l3 = msg(vec![(1, Field::Present(Value::Message(leaf)))]);
    let l2 = msg(vec![(1, Field::Present(Value::Message(l3)))]);
    let l1 = msg(vec![(1, Field::Present(Value::Message(l2)))]);
    let root = msg(vec![(1, Field::Present(Value::Message(l1)))]);

    let mut buf = Vec::new();
    encode_envelope(&s, &root, &mut buf, EncodeLimit::default()).unwrap();

    let shallow = Limits {
        max_value_len: 1 << 20,
        max_output: 1 << 20,
        max_depth: 2,
    };
    let mut cursor = std::io::Cursor::new(buf);
    let err = decode_envelope(&s, &mut cursor, shallow).unwrap_err();
    assert!(matches!(err, bse::Error::NestingTooDeep { limit: 2 }));
}

#[test]
fn valid_data_decodes_with_permissive_limits() {
    let bytes = encode_hello();
    let limits = Limits {
        max_value_len: 1 << 20,
        max_output: 1 << 20,
        max_depth: 8,
    };
    let mut cursor = std::io::Cursor::new(bytes);
    let d = decode_envelope(&person_schema(), &mut cursor, limits).unwrap();
    assert_eq!(
        d.message.fields.get(&1),
        Some(&Field::Present(Value::Int(1)))
    );
}
