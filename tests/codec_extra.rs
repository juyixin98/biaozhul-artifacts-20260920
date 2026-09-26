//! Packed/unpacked compatibility, scalar coverage and malformed-value tests.

mod common;

use bse::codec::{decode_envelope, encode_envelope, EncodeLimit};
use bse::schema::{Cardinality, ScalarType};
use bse::value::{Field, Value};
use bse::wire::{write_envelope, write_varint, Limits, WireType, WireWriter};
use common::{bytes as bval, msg, present, repeated, scalar, schema, AsValue};

fn nums_schema(packed: bool) -> bse::schema::Schema {
    let mut f = scalar("nums", 1, ScalarType::Int32, Cardinality::Repeated);
    f.packed = packed;
    schema("M", vec![("M", vec![f])])
}

fn encode(s: &bse::schema::Schema, m: &bse::value::Message) -> Vec<u8> {
    let mut buf = Vec::new();
    encode_envelope(s, m, &mut buf, EncodeLimit::default()).unwrap();
    buf
}

fn decode(s: &bse::schema::Schema, raw: &[u8]) -> bse::codec::Decoded {
    let mut cursor = std::io::Cursor::new(raw.to_vec());
    decode_envelope(s, &mut cursor, Limits::default()).unwrap()
}

#[test]
fn packed_writer_read_by_unpacked_reader_and_vice_versa() {
    let values = vec![
        Value::Int(0),
        Value::Int(-1),
        Value::Int(127),
        Value::Int(-128),
    ];
    let message = msg(vec![(1, repeated(values.clone()))]);

    let packed_bytes = encode(&nums_schema(true), &message);
    let unpacked_bytes = encode(&nums_schema(false), &message);

    // Packed bytes read by an unpacked-expecting schema.
    let a = decode(&nums_schema(false), &packed_bytes);
    assert_eq!(
        a.message.fields.get(&1),
        Some(&Field::Repeated(values.clone()))
    );

    // Unpacked bytes read by a packed-expecting schema.
    let b = decode(&nums_schema(true), &unpacked_bytes);
    assert_eq!(b.message.fields.get(&1), Some(&Field::Repeated(values)));

    // And the two encodings really do differ on the wire.
    assert_ne!(packed_bytes, unpacked_bytes);
}

#[test]
fn all_scalar_types_round_trip() {
    let s = schema(
        "M",
        vec![(
            "M",
            vec![
                scalar("i32", 1, ScalarType::Int32, Cardinality::Optional),
                scalar("i64", 2, ScalarType::Int64, Cardinality::Optional),
                scalar("u32", 3, ScalarType::Uint32, Cardinality::Optional),
                scalar("u64", 4, ScalarType::Uint64, Cardinality::Optional),
                scalar("f32", 5, ScalarType::Float, Cardinality::Optional),
                scalar("f64", 6, ScalarType::Double, Cardinality::Optional),
                scalar("flag", 7, ScalarType::Bool, Cardinality::Optional),
                scalar("text", 8, ScalarType::String, Cardinality::Optional),
                scalar("data", 9, ScalarType::Bytes, Cardinality::Optional),
            ],
        )],
    );
    let m = msg(vec![
        (1, present(-12345_i32)),
        (2, present(-9_000_000_001_i64)),
        (3, present(42_u64)),
        (4, present(u64::MAX)),
        (5, Field::Present(Value::Float(1.5))),
        (6, Field::Present(Value::Double(-2.25))),
        (7, present(true)),
        (8, present("héllo 🦀")),
        (9, Field::Present(bval(vec![0, 255, 1, 2]))),
    ]);
    let (back, _) = common::round_trip(&s, &m);
    assert_eq!(
        back.fields.get(&1),
        Some(&Field::Present(Value::Int(-12345)))
    );
    assert_eq!(
        back.fields.get(&2),
        Some(&Field::Present(Value::Int(-9_000_000_001)))
    );
    assert_eq!(back.fields.get(&3), Some(&Field::Present(Value::Uint(42))));
    assert_eq!(
        back.fields.get(&4),
        Some(&Field::Present(Value::Uint(u64::MAX)))
    );
    assert_eq!(
        back.fields.get(&5),
        Some(&Field::Present(Value::Float(1.5)))
    );
    assert_eq!(
        back.fields.get(&6),
        Some(&Field::Present(Value::Double(-2.25)))
    );
    assert_eq!(
        back.fields.get(&7),
        Some(&Field::Present(Value::Bool(true)))
    );
    assert_eq!(back.fields.get(&8), Some(&Field::Present("héllo 🦀".v())));
    assert_eq!(
        back.fields.get(&9),
        Some(&Field::Present(bval(vec![0, 255, 1, 2])))
    );
}

#[test]
fn int32_rejects_a_value_that_does_not_fit() {
    // int32 field carrying a varint that only fits int64.
    let mut payload = Vec::new();
    {
        let mut w = WireWriter::new(&mut payload);
        w.tag(1, WireType::Varint).unwrap();
        w.varint(1u64 << 40).unwrap();
    }
    let mut raw = Vec::new();
    write_envelope(&mut raw, &payload).unwrap();

    let s = schema(
        "M",
        vec![(
            "M",
            vec![scalar("x", 1, ScalarType::Int32, Cardinality::Optional)],
        )],
    );
    let mut cursor = std::io::Cursor::new(raw);
    let err = decode_envelope(&s, &mut cursor, Limits::default()).unwrap_err();
    assert!(matches!(err, bse::Error::IncompatibleEvolution { .. }));
}

#[test]
fn bool_rejects_values_other_than_0_or_1() {
    let mut payload = Vec::new();
    {
        let mut w = WireWriter::new(&mut payload);
        w.tag(1, WireType::Varint).unwrap();
        w.varint(2).unwrap();
    }
    let mut raw = Vec::new();
    write_envelope(&mut raw, &payload).unwrap();

    let s = schema(
        "M",
        vec![(
            "M",
            vec![scalar("b", 1, ScalarType::Bool, Cardinality::Optional)],
        )],
    );
    let mut cursor = std::io::Cursor::new(raw);
    let err = decode_envelope(&s, &mut cursor, Limits::default()).unwrap_err();
    assert!(matches!(err, bse::Error::MalformedValue(_)));
}

#[test]
fn string_rejects_non_utf8_bytes() {
    let mut payload = Vec::new();
    {
        let mut w = WireWriter::new(&mut payload);
        w.tag(1, WireType::Len).unwrap();
        w.len_bytes(&[0xff, 0xfe, 0xfd]).unwrap();
    }
    let mut raw = Vec::new();
    write_envelope(&mut raw, &payload).unwrap();

    let s = schema(
        "M",
        vec![(
            "M",
            vec![scalar("t", 1, ScalarType::String, Cardinality::Optional)],
        )],
    );
    let mut cursor = std::io::Cursor::new(raw);
    let err = decode_envelope(&s, &mut cursor, Limits::default()).unwrap_err();
    assert!(matches!(err, bse::Error::MalformedValue(_)));
}

#[test]
fn float32_and_double_preserve_bit_patterns() {
    let s = schema(
        "M",
        vec![(
            "M",
            vec![
                scalar("f", 1, ScalarType::Float, Cardinality::Optional),
                scalar("d", 2, ScalarType::Double, Cardinality::Optional),
            ],
        )],
    );
    let m = msg(vec![
        (1, Field::Present(Value::Float(f32::MIN_POSITIVE))),
        (2, Field::Present(Value::Double(f64::MIN_POSITIVE))),
    ]);
    let (back, _) = common::round_trip(&s, &m);
    assert_eq!(
        back.fields.get(&1),
        Some(&Field::Present(Value::Float(f32::MIN_POSITIVE)))
    );
    assert_eq!(
        back.fields.get(&2),
        Some(&Field::Present(Value::Double(f64::MIN_POSITIVE)))
    );
}

#[test]
fn empty_repeated_is_absent_on_the_wire_but_distinct_from_missing_in_value_model() {
    // An empty repeated encodes to zero bytes and decodes to no field,
    // while the in-memory model can still hold an explicit empty list.
    let s = nums_schema(true);
    let m = msg(vec![(1, repeated(vec![]))]);
    let bytes = encode(&s, &m);
    let d = decode(&s, &bytes);
    assert!(!d.message.fields.contains_key(&1));
    assert_eq!(m.fields.get(&1), Some(&Field::Repeated(vec![])));
}

#[test]
fn varint_helpers_are_used_consistently() {
    let mut buf = Vec::new();
    write_varint(&mut buf, 300);
    assert_eq!(buf, vec![0xac, 0x02]);
    assert_eq!(bse::wire::parse_varint(&buf), (300, 2));
}
