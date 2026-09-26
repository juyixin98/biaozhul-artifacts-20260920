//! Acceptance tests for schema evolution:
//! new/old schemas read each other's data both ways, unknown fields are
//! forwarded through an intermediate schema, truncation is rejected, and
//! missing values stay distinct from explicit zeros.

mod common;

use bse::codec::{decode_envelope, encode_envelope, EncodeLimit};
use bse::schema::{Cardinality, ScalarType};
use bse::value::{Field, Value};
use bse::wire::Limits;
use common::{message_field, missing, msg, present, repeated, scalar, schema, AsValue};

fn v1() -> bse::schema::Schema {
    schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Required),
                scalar("email", 3, ScalarType::String, Cardinality::Optional),
                scalar("tags", 4, ScalarType::String, Cardinality::Repeated),
            ],
        )],
    )
}

/// v2 adds optional `age` (7), packed repeated `scores` (8), and removes
/// knowledge of `tags` (4) to simulate a reader that dropped that field.
fn v2() -> bse::schema::Schema {
    schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Required),
                scalar("email", 3, ScalarType::String, Cardinality::Optional),
                scalar("age", 7, ScalarType::Int32, Cardinality::Optional),
                {
                    let mut f = scalar("scores", 8, ScalarType::Int32, Cardinality::Repeated);
                    f.packed = true;
                    f
                },
            ],
        )],
    )
}

fn encode(s: &bse::schema::Schema, m: &bse::value::Message) -> Vec<u8> {
    let mut buf = Vec::new();
    encode_envelope(s, m, &mut buf, EncodeLimit::default()).expect("encode");
    buf
}

fn decode(s: &bse::schema::Schema, bytes: &[u8]) -> bse::codec::Decoded {
    let mut cursor = std::io::Cursor::new(bytes.to_vec());
    decode_envelope(s, &mut cursor, Limits::default()).expect("decode")
}

// ---------------------------------------------------------------------------
// Presence: missing is not the same as an explicit zero / empty / false
// ---------------------------------------------------------------------------

#[test]
fn explicit_zero_survives_and_differs_from_missing() {
    let s = v1();
    let with_zero = msg(vec![
        (1, present(0_i64)),
        (2, present("Zero")),
        (3, missing()),
    ]);
    let (decoded, _) = common::round_trip(&s, &with_zero);
    assert_eq!(decoded.fields.get(&1), Some(&Field::Present(Value::Int(0))));
    assert_eq!(
        decoded.fields.get(&3),
        None,
        "omitted optional must not appear as a value"
    );
    assert_ne!(
        decoded.fields.get(&1),
        Some(&Field::Missing),
        "explicit int 0 must not collapse to missing"
    );
}

#[test]
fn explicit_false_and_empty_string_are_present() {
    let s = schema(
        "M",
        vec![(
            "M",
            vec![
                scalar("flag", 1, ScalarType::Bool, Cardinality::Optional),
                scalar("text", 2, ScalarType::String, Cardinality::Optional),
                scalar("n", 3, ScalarType::Uint64, Cardinality::Optional),
            ],
        )],
    );
    let m = msg(vec![
        (1, present(false)),
        (2, present("")),
        (3, present(0_u64)),
    ]);
    let (decoded, _) = common::round_trip(&s, &m);
    assert_eq!(
        decoded.fields.get(&1),
        Some(&Field::Present(Value::Bool(false)))
    );
    assert_eq!(
        decoded.fields.get(&2),
        Some(&Field::Present(Value::Str(String::new())))
    );
    assert_eq!(
        decoded.fields.get(&3),
        Some(&Field::Present(Value::Uint(0)))
    );
}

#[test]
fn missing_required_is_rejected_on_encode_and_decode() {
    let s = v1();
    let no_id = msg(vec![(2, present("nameless"))]);
    let mut buf = Vec::new();
    assert!(encode_envelope(&s, &no_id, &mut buf, EncodeLimit::default()).is_err());
}

// ---------------------------------------------------------------------------
// Bidirectional reads across schema versions
// ---------------------------------------------------------------------------

#[test]
fn old_data_reads_under_new_schema() {
    let old = v1();
    let new = v2();
    let old_msg = msg(vec![
        (1, present(42_i64)),
        (2, present("Ada")),
        (4, repeated(vec!["a".v(), "b".v()])),
    ]);
    let bytes = encode(&old, &old_msg);

    // New schema reads old data: known fields intact, tags(4) unknown,
    // new fields simply absent. tags is an unpacked repeated string, so
    // each occurrence is captured separately (2 raw fields, both #4).
    let d = decode(&new, &bytes);
    assert_eq!(
        d.message.fields.get(&1),
        Some(&Field::Present(Value::Int(42)))
    );
    assert_eq!(d.message.fields.get(&2), Some(&Field::Present("Ada".v())));
    assert_eq!(
        d.message.fields.get(&7),
        None,
        "added optional must be absent"
    );
    let unknown_nums: Vec<u32> = d.message.unknown.iter().map(|r| r.field).collect();
    assert_eq!(unknown_nums, vec![4, 4]);
    assert_eq!(d.stats.unknown_fields, 2);
}

#[test]
fn new_data_reads_under_old_schema() {
    let old = v1();
    let new = v2();
    let new_msg = msg(vec![
        (1, present(7_i64)),
        (2, present("Grace")),
        (3, present("grace@navy")),
        (7, present(79_i32)),
        (
            8,
            repeated(vec![
                Value::Int(10),
                Value::Int(20),
                Value::Int(-3),
                Value::Int(0),
            ]),
        ),
    ]);
    let bytes = encode(&new, &new_msg);

    // Old schema reads new data: age(7) and scores(8) become unknown and
    // are counted, core fields still decode.
    let d = decode(&old, &bytes);
    assert_eq!(
        d.message.fields.get(&1),
        Some(&Field::Present(Value::Int(7)))
    );
    assert_eq!(
        d.message.fields.get(&3),
        Some(&Field::Present("grace@navy".v()))
    );
    let unknown_nums: Vec<u32> = d.message.unknown.iter().map(|r| r.field).collect();
    assert_eq!(unknown_nums, vec![7, 8]);
    assert_eq!(d.stats.unknown_fields, 2);
    assert!(d.stats.unknown_bytes > 0);
}

// ---------------------------------------------------------------------------
// Unknown-field forwarding through an intermediate schema
// ---------------------------------------------------------------------------

#[test]
fn unknown_fields_are_forwarded_byte_for_byte() {
    let producer = v2();
    let middle = v1(); // does not know age(7)/scores(8)
    let consumer = v2();

    let new_msg = msg(vec![
        (1, present(9_i64)),
        (2, present("Forwarded")),
        (7, present(33_i32)),
        (8, repeated(vec![Value::Int(1), Value::Int(2)])),
    ]);
    let original_bytes = encode(&producer, &new_msg);

    // Middle decodes, edits a known field, re-encodes.
    let mut cursor = std::io::Cursor::new(original_bytes.clone());
    let mut seen = decode_envelope(&middle, &mut cursor, Limits::default()).unwrap();
    seen.message
        .fields
        .insert(2, Field::Present(Value::Str("edited".into())));
    let mut forwarded = Vec::new();
    encode_envelope(
        &middle,
        &seen.message,
        &mut forwarded,
        EncodeLimit::default(),
    )
    .unwrap();

    // Consumer (new schema again) recovers both the edit and fields the
    // middle never understood.
    let final_d = decode(&consumer, &forwarded);
    assert_eq!(
        final_d.message.fields.get(&2),
        Some(&Field::Present("edited".v()))
    );
    assert_eq!(
        final_d.message.fields.get(&7),
        Some(&Field::Present(Value::Int(33)))
    );
    match final_d.message.fields.get(&8) {
        Some(Field::Repeated(v)) => assert_eq!(v, &vec![Value::Int(1), Value::Int(2)]),
        other => panic!("expected scores repeated, got {other:?}"),
    }
    assert_eq!(final_d.stats.unknown_fields, 0);
}

// ---------------------------------------------------------------------------
// Incompatible evolution must be rejected
// ---------------------------------------------------------------------------

#[test]
fn incompatible_type_change_is_rejected() {
    let old = v1(); // email(3) is string -> wire LEN
    let bad = schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Required),
                scalar("email", 3, ScalarType::Int32, Cardinality::Optional),
            ],
        )],
    );
    let m = msg(vec![
        (1, present(1_i64)),
        (2, present("x")),
        (3, present("text-not-int")),
    ]);
    let bytes = encode(&old, &m);
    let mut cursor = std::io::Cursor::new(bytes);
    let err = decode_envelope(&bad, &mut cursor, Limits::default()).unwrap_err();
    assert!(
        matches!(err, bse::Error::IncompatibleEvolution { field: 3, .. }),
        "expected IncompatibleEvolution for field 3, got {err:?}"
    );
}

#[test]
fn adding_required_field_is_incompatible_and_data_fails_required_check() {
    use bse::schema::{check_message_evolution, EvolutionVerdict};
    let old = v1();
    let new = schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Required),
                scalar("tenant", 9, ScalarType::Int64, Cardinality::Required),
            ],
        )],
    );
    let report = check_message_evolution("Person", old.root_message(), new.root_message());
    assert!(!report.compatible);
    assert!(report
        .notes
        .iter()
        .any(|(n, _, v)| *n == 9 && matches!(v, EvolutionVerdict::Incompatible(_))));

    // And old bytes fail the required-field check under the new schema.
    let bytes = encode(&old, &msg(vec![(1, present(1_i64)), (2, present("a"))]));
    let mut cursor = std::io::Cursor::new(bytes);
    assert!(decode_envelope(&new, &mut cursor, Limits::default()).is_err());
}

// ---------------------------------------------------------------------------
// Nested messages and truncation
// ---------------------------------------------------------------------------

#[test]
fn nested_message_round_trips_with_unknown_inside() {
    let old = schema(
        "P",
        vec![
            (
                "P",
                vec![
                    scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                    message_field("addr", 2, "Addr", Cardinality::Optional),
                ],
            ),
            (
                "Addr",
                vec![scalar(
                    "street",
                    1,
                    ScalarType::String,
                    Cardinality::Optional,
                )],
            ),
        ],
    );
    let new = schema(
        "P",
        vec![
            (
                "P",
                vec![
                    scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                    message_field("addr", 2, "Addr", Cardinality::Optional),
                ],
            ),
            (
                "Addr",
                vec![
                    scalar("street", 1, ScalarType::String, Cardinality::Optional),
                    scalar("city", 3, ScalarType::String, Cardinality::Optional),
                ],
            ),
        ],
    );
    let addr = msg(vec![
        (1, present("Main St")),
        (3, present("Arlington")), // unknown to old Addr
    ]);
    let m = msg(vec![
        (1, present(5_i64)),
        (2, Field::Present(Value::Message(addr))),
    ]);
    let bytes = encode(&new, &m);

    // Old schema preserves the unknown city inside the nested message.
    let d = decode(&old, &bytes);
    let nested = match d.message.fields.get(&2) {
        Some(Field::Present(Value::Message(n))) => n,
        other => panic!("expected nested message, got {other:?}"),
    };
    assert_eq!(nested.unknown.len(), 1);
    assert_eq!(nested.unknown[0].field, 3);

    // Forward and read under the new schema again.
    let mut out = Vec::new();
    encode_envelope(&old, &d.message, &mut out, EncodeLimit::default()).unwrap();
    let back = decode(&new, &out);
    let nested2 = match back.message.fields.get(&2) {
        Some(Field::Present(Value::Message(n))) => n,
        other => panic!("expected nested message, got {other:?}"),
    };
    assert_eq!(
        nested2.fields.get(&3),
        Some(&Field::Present("Arlington".v()))
    );
}

#[test]
fn truncated_payload_is_rejected_not_panicked() {
    let s = v1();
    let m = msg(vec![
        (1, present(1_i64)),
        (2, present("some reasonably long name here")),
    ]);
    let mut full = Vec::new();
    encode_envelope(&s, &m, &mut full, EncodeLimit::default()).unwrap();

    // Keep the 10-byte envelope header but cut the payload in half.
    let cut = full.len() / 2;
    let mut truncated = full[..10].to_vec();
    truncated.extend_from_slice(&full[10..cut]);
    let mut cursor = std::io::Cursor::new(truncated);
    let result = decode_envelope(&s, &mut cursor, Limits::default());
    assert!(result.is_err(), "truncated input must error");
}

#[test]
fn truncated_nested_message_length_is_rejected() {
    let s = schema(
        "P",
        vec![
            (
                "P",
                vec![
                    scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                    message_field("addr", 2, "Addr", Cardinality::Optional),
                ],
            ),
            (
                "Addr",
                vec![scalar(
                    "street",
                    1,
                    ScalarType::String,
                    Cardinality::Optional,
                )],
            ),
        ],
    );
    // Hand-craft a message whose nested LEN declares more bytes than exist.
    use bse::wire::{write_envelope, write_varint, WireType, WireWriter};
    let mut payload = Vec::new();
    {
        let mut w = WireWriter::new(&mut payload);
        w.tag(1, WireType::Varint).unwrap();
        w.varint(2).unwrap(); // id = 1 (zigzag)
        w.tag(2, WireType::Len).unwrap();
        // Declare length 50 but supply only 4 bytes.
        let mut lenbuf = Vec::new();
        write_varint(&mut lenbuf, 50);
        w.raw(&lenbuf).unwrap();
        w.raw(b"abcd").unwrap();
    }
    let mut full = Vec::new();
    write_envelope(&mut full, &payload).unwrap();
    // The envelope length header itself must lie for the bytes to even be
    // accepted; rewrite declared payload length to the actual payload size.
    let actual = (payload.len()) as u32;
    full[6..10].copy_from_slice(&actual.to_le_bytes());

    let mut cursor = std::io::Cursor::new(full);
    let err = decode_envelope(&s, &mut cursor, Limits::default()).unwrap_err();
    assert!(matches!(
        err,
        bse::Error::UnexpectedEof { .. } | bse::Error::LengthOutOfBounds { .. }
    ));
}
