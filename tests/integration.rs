//! Acceptance tests for BSE1 schema evolution.
//!
//! Coverage:
//! - full scalar round-trips (incl. >2^53 integers, special doubles)
//! - explicit zero vs. missing optional field
//! - new schema reads old data; old schema reads new data
//! - unknown-field forwarding through an intermediate schema
//! - packed <-> unpacked reading
//! - incompatible type evolution rejected
//! - int32/int64 width evolution accepted/rejected by value range
//! - truncation at every cut point
//! - resource limits (bytes, depth, repeated count)
//! - JSON request envelope

use bschema::json::{parse, Json};
use bschema::limits::Limits;
use bschema::reader;
use bschema::schema::Schema;
use bschema::value::{self, MessageValue};
use bschema::writer;
use bschema::{decode, encode};

// ---------------------------------------------------------------------------
// Fixture schemas
// ---------------------------------------------------------------------------

const PERSON_V1: &str = r#"{
  "root": "Person",
  "messages": [
    {
      "name": "Address",
      "fields": [
        {"number": 1, "name": "street", "type": "string", "required": true},
        {"number": 2, "name": "zip", "type": "int32"}
      ]
    },
    {
      "name": "Person",
      "fields": [
        {"number": 1, "name": "id", "type": "int32", "required": true},
        {"number": 2, "name": "name", "type": "string", "required": true},
        {"number": 3, "name": "email", "type": "string"},
        {"number": 4, "name": "scores", "type": "sint32", "repeated": "packed"},
        {"number": 5, "name": "address", "type": "Address"}
      ]
    }
  ]
}"#;

/// V2: adds `nickname` (6) and unpacked `tags` (7); widens id to int64.
const PERSON_V2: &str = r#"{
  "root": "Person",
  "messages": [
    {
      "name": "Address",
      "fields": [
        {"number": 1, "name": "street", "type": "string", "required": true},
        {"number": 2, "name": "zip", "type": "int32"}
      ]
    },
    {
      "name": "Person",
      "fields": [
        {"number": 1, "name": "id", "type": "int64", "required": true},
        {"number": 2, "name": "name", "type": "string", "required": true},
        {"number": 3, "name": "email", "type": "string"},
        {"number": 4, "name": "scores", "type": "sint32", "repeated": "packed"},
        {"number": 5, "name": "address", "type": "Address"},
        {"number": 6, "name": "nickname", "type": "string"},
        {"number": 7, "name": "tags", "type": "string", "repeated": "unpacked"}
      ]
    }
  ]
}"#;

/// V1 prime: declares scores unpacked instead of packed (strategy change).
const PERSON_V1_UNPACKED: &str = r#"{
  "root": "Person",
  "messages": [
    {
      "name": "Address",
      "fields": [
        {"number": 1, "name": "street", "type": "string", "required": true},
        {"number": 2, "name": "zip", "type": "int32"}
      ]
    },
    {
      "name": "Person",
      "fields": [
        {"number": 1, "name": "id", "type": "int32", "required": true},
        {"number": 2, "name": "name", "type": "string", "required": true},
        {"number": 3, "name": "email", "type": "string"},
        {"number": 4, "name": "scores", "type": "sint32", "repeated": "unpacked"},
        {"number": 5, "name": "address", "type": "Address"}
      ]
    }
  ]
}"#;

/// Incompatible evolution: field 3 changes string -> int64.
const PERSON_BAD_TYPE: &str = r#"{
  "root": "Person",
  "messages": [
    {
      "name": "Person",
      "fields": [
        {"number": 1, "name": "id", "type": "int32", "required": true},
        {"number": 2, "name": "name", "type": "string", "required": true},
        {"number": 3, "name": "email", "type": "int64"}
      ]
    }
  ]
}"#;

fn schema(json_text: &str) -> Schema {
    Schema::from_json(&parse(json_text).unwrap()).unwrap()
}

fn enc(schema: &Schema, msg_json: &str) -> Vec<u8> {
    let m = value::json_to_message(schema, &parse(msg_json).unwrap()).unwrap();
    encode(schema, &m, &Limits::default()).unwrap()
}

// ---------------------------------------------------------------------------
// Round-trips
// ---------------------------------------------------------------------------

#[test]
fn roundtrip_all_scalar_types() {
    let schema_json = r#"{
      "root": "R",
      "messages": [{ "name": "R", "fields": [
        {"number": 1, "name": "a", "type": "int64"},
        {"number": 2, "name": "b", "type": "sint64"},
        {"number": 3, "name": "c", "type": "fixed64"},
        {"number": 4, "name": "d", "type": "int32"},
        {"number": 5, "name": "e", "type": "sint32"},
        {"number": 6, "name": "f", "type": "fixed32"},
        {"number": 7, "name": "g", "type": "bool"},
        {"number": 8, "name": "h", "type": "double"},
        {"number": 9, "name": "i", "type": "string"},
        {"number": 10, "name": "j", "type": "bytes"}
      ]}]
    }"#;
    let s = schema(schema_json);
    // Values beyond 2^53 prove integer precision is preserved.
    let msg = r#"{
      "a": 9007199254740993,
      "b": -9007199254740993,
      "c": 18446744073709551615,
      "d": 2147483647,
      "e": -2147483648,
      "f": 4294967295,
      "g": true,
      "h": 3.141592653589793,
      "i": "héllo\nBSE",
      "j": "AAECAv///8A="
    }"#;
    let bin = enc(&s, msg);
    let out = decode(&s, &bin, &Limits::default()).unwrap();
    let j = value::message_to_json(&s, &out.message);
    let text = bschema::json::to_string_pretty(&j).unwrap();
    assert!(text.contains("9007199254740993"), "{text}");
    assert!(text.contains("-9007199254740993"), "{text}");
    assert!(text.contains("18446744073709551615"), "{text}");
    assert!(text.contains("2147483647"), "{text}");
    assert!(text.contains("-2147483648"), "{text}");
    assert!(text.contains("4294967295"), "{text}");
    assert!(text.contains("AAECAv///8A="), "{text}");
}

#[test]
fn roundtrip_special_doubles() {
    let s = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
           {"number":1,"name":"x","type":"double"}]}]}"#,
    );
    for v in [
        "0",
        "-0.0",
        "1e308",
        "2.2250738585072014e-308",
        "1.7976931348623157e308",
    ] {
        let bin = enc(&s, &format!(r#"{{"x":{v}}}"#));
        let out = decode(&s, &bin, &Limits::default()).unwrap();
        let j = value::message_to_json(&s, &out.message);
        let reparsed = bschema::json::to_string_pretty(&j).unwrap();
        // Re-decoding the JSON number yields the same f64 bits.
        let again = parse(&reparsed).unwrap();
        let f1 = again.get("x").unwrap().as_f64().unwrap().to_bits();
        let f0 = parse(v).unwrap().as_f64().unwrap().to_bits();
        assert_eq!(f0, f1, "double {v} did not round-trip");
    }
}

#[test]
fn explicit_zero_is_distinct_from_missing() {
    let s = schema(PERSON_V1);

    // `email` absent, `address.zip` absent: keys must be omitted.
    let missing = r#"{"id": 0, "name": ""}"#;
    // id is present-with-zero (required, encoded); name present empty string.
    let bin = enc(&s, missing);
    let out = decode(&s, &bin, &Limits::default()).unwrap();
    let j = value::message_to_json(&s, &out.message);
    let obj = j.as_object().unwrap();
    let keys: Vec<&str> = obj.iter().map(|(k, _)| k.as_str()).collect();
    assert!(keys.contains(&"id"), "explicit id=0 must appear: {keys:?}");
    assert!(
        keys.contains(&"name"),
        "explicit name=\"\" must appear: {keys:?}"
    );
    assert!(
        !keys.contains(&"email"),
        "missing email must be omitted: {keys:?}"
    );
    assert!(
        !keys.contains(&"scores"),
        "missing repeated must be omitted: {keys:?}"
    );
    assert!(
        !keys.contains(&"address"),
        "missing message must be omitted: {keys:?}"
    );

    // Programmatic check: id present as zero, not Absent.
    match out.message.fields.get(&1).unwrap() {
        bschema::value::FieldValue::Present(bschema::Value::Int32(0)) => {}
        other => panic!("expected Present(Int32(0)), got {other:?}"),
    }
    assert!(!out.message.fields.contains_key(&3));

    // Empty repeated array encodes nothing and decodes as absent.
    let bin2 = enc(&s, r#"{"id": 1, "name": "a", "scores": []}"#);
    let out2 = decode(&s, &bin2, &Limits::default()).unwrap();
    assert!(!out2.message.fields.contains_key(&4));
}

#[test]
fn required_missing_is_rejected_on_both_sides() {
    let s = schema(PERSON_V1);
    let m = value::json_to_message(&s, &parse(r#"{"name": "n"}"#).unwrap());
    assert!(
        m.is_err(),
        "missing required id rejected at JSON conversion"
    );

    // Also enforced when encoding a hand-built value.
    let bin = enc(&s, r#"{"id": 1, "name": "n"}"#);
    // Flip the wire bytes to drop field 1 is complex; instead decode a
    // truncated-to-only-name message by constructing with a reduced schema.
    let name_only = schema(
        r#"{"root":"P","messages":[{"name":"P","fields":[
           {"number":2,"name":"name","type":"string"}]}]}"#,
    );
    let orphan = enc(&name_only, r#"{"name":"n"}"#);
    // Strip the 14-byte header and rebuild with the V1 header so V1 rejects
    // the missing required field 1.
    let mut doctored = bin[..14].to_vec();
    doctored.extend_from_slice(&orphan[14..]);
    let err = decode(&s, &doctored, &Limits::default()).unwrap_err();
    assert!(
        matches!(err, bschema::Error::MissingRequired { number: 1, .. }),
        "got {err:?}"
    );
}

// ---------------------------------------------------------------------------
// Evolution: new reads old, old reads new
// ---------------------------------------------------------------------------

#[test]
fn new_schema_reads_old_data() {
    let v1 = schema(PERSON_V1);
    let v2 = schema(PERSON_V2);

    let bin = enc(
        &v1,
        r#"{"id": 7, "name": "ada", "email": "a@x.io",
            "scores": [-1, 2, -300], "address": {"street": "1 Main", "zip": 1000}}"#,
    );
    let out = reader::decode(&v2, &bin, &Limits::default()).unwrap();
    assert!(!out.fingerprint_match(), "fingerprints must differ");
    let j = value::message_to_json(&v2, &out.message);
    let text = bschema::json::to_string_pretty(&j).unwrap();
    assert!(text.contains("\"ada\""));
    assert!(
        !text.contains("nickname"),
        "added field must be absent, not null"
    );
    assert!(!text.contains("__unknown_fields__"));
}

#[test]
fn old_schema_reads_new_data_and_keeps_unknown() {
    let v1 = schema(PERSON_V1);
    let v2 = schema(PERSON_V2);

    let bin = enc(
        &v2,
        r#"{"id": 7, "name": "ada", "nickname": "A",
            "tags": ["admin", "ops"], "scores": [10]}"#,
    );
    let out = reader::decode(&v1, &bin, &Limits::default()).unwrap();
    // nickname is one entry; unpacked `tags` produces one unknown entry per
    // value (two total).
    assert_eq!(
        out.message.unknown.len(),
        3,
        "nickname + 2 tag entries retained"
    );
    let nums: Vec<u32> = out.message.unknown.iter().map(|u| u.number).collect();
    assert_eq!(nums.iter().filter(|&&n| n == 6).count(), 1);
    assert_eq!(nums.iter().filter(|&&n| n == 7).count(), 2);

    // Unknown length-delimited payload is the region WITHOUT its length
    // prefix (the writer re-adds it on forwarding; the byte-exact forwarding
    // test below proves round-trip fidelity).
    let nick = out.message.unknown.iter().find(|u| u.number == 6).unwrap();
    assert_eq!(nick.payload, b"A", "STRING region 'A'");
}

#[test]
fn unknown_fields_are_forwarded_through_intermediate_reader() {
    let v1 = schema(PERSON_V1);
    let v2 = schema(PERSON_V2);

    let original = enc(
        &v2,
        r#"{"id": 9, "name": "grace", "email": "g@x.io",
            "nickname": "Amazing Grace", "tags": ["a","b","c"],
            "scores": [100, -200], "address": {"street": "S", "zip": 5}}"#,
    );

    // V1 proxy: decode then re-encode.
    let seen_by_v1 = reader::decode(&v1, &original, &Limits::default()).unwrap();
    let forwarded = writer::encode(&v1, &seen_by_v1.message, &Limits::default()).unwrap();

    // V2 consumer recovers everything.
    let final_out = reader::decode(&v2, &forwarded, &Limits::default()).unwrap();
    let j = value::message_to_json(&v2, &final_out.message);
    let text = bschema::json::to_string_pretty(&j).unwrap();
    assert!(text.contains("Amazing Grace"), "{text}");
    assert!(text.contains("\"a\"") && text.contains("\"b\"") && text.contains("\"c\""));
    assert!(text.contains("g@x.io"));
    assert!(
        !text.contains("__unknown_fields__"),
        "V2 must know all fields after forwarding: {text}"
    );

    // The retained unknown payloads in the V1 view are byte-identical to the
    // payloads V2 originally wrote (find them in the original via a raw
    // unknown capture with an empty schema).
    let empty = schema(r#"{"root":"P","messages":[{"name":"P","fields":[]}]}"#);
    let raw_view = reader::decode(&empty, &original, &Limits::default()).unwrap();
    let fwd_view = reader::decode(&empty, &forwarded, &Limits::default()).unwrap();
    let sort = |mut v: Vec<(u32, Vec<u8>)>| {
        v.sort_by_key(|(n, _)| *n);
        v
    };
    let orig_pairs = sort(
        raw_view
            .message
            .unknown
            .iter()
            .map(|u| (u.number, u.payload.clone()))
            .collect(),
    );
    let fwd_pairs = sort(
        fwd_view
            .message
            .unknown
            .iter()
            .map(|u| (u.number, u.payload.clone()))
            .collect(),
    );
    assert_eq!(
        orig_pairs, fwd_pairs,
        "forwarded payloads must be byte-exact"
    );
}

#[test]
fn unknown_field_inside_nested_message_is_forwarded() {
    let addr_old = schema(
        r#"{"root":"P","messages":[
          {"name":"A","fields":[{"number":1,"name":"street","type":"string"}]},
          {"name":"P","fields":[
            {"number":1,"name":"id","type":"int32","required":true},
            {"number":2,"name":"addr","type":"A"}]}]}"#,
    );
    let addr_new = schema(
        r#"{"root":"P","messages":[
          {"name":"A","fields":[
            {"number":1,"name":"street","type":"string"},
            {"number":3,"name":"country","type":"string"}]},
          {"name":"P","fields":[
            {"number":1,"name":"id","type":"int32","required":true},
            {"number":2,"name":"addr","type":"A"}]}]}"#,
    );
    let bin = enc(
        &addr_new,
        r#"{"id":1,"addr":{"street":"s","country":"NL"}}"#,
    );
    let seen = reader::decode(&addr_old, &bin, &Limits::default()).unwrap();
    let fwd = writer::encode(&addr_old, &seen.message, &Limits::default()).unwrap();
    let out = reader::decode(&addr_new, &fwd, &Limits::default()).unwrap();
    let text =
        bschema::json::to_string_pretty(&value::message_to_json(&addr_new, &out.message)).unwrap();
    assert!(text.contains("NL"), "{text}");
}

// ---------------------------------------------------------------------------
// Repetition strategies
// ---------------------------------------------------------------------------

#[test]
fn packed_data_is_read_by_unpacked_schema_and_vice_versa() {
    let packed = schema(PERSON_V1);
    let unpacked = schema(PERSON_V1_UNPACKED);

    let bin_packed = enc(&packed, r#"{"id":1,"name":"n","scores":[1,-2,300]}"#);
    let out = reader::decode(&unpacked, &bin_packed, &Limits::default()).unwrap();
    let j = value::message_to_json(&unpacked, &out.message);
    assert!(bschema::json::to_string_pretty(&j).unwrap().contains("300"));

    let bin_unpacked = enc(&unpacked, r#"{"id":1,"name":"n","scores":[-7,8]}"#);
    let out2 = reader::decode(&packed, &bin_unpacked, &Limits::default()).unwrap();
    match out2.message.fields.get(&4).unwrap() {
        bschema::value::FieldValue::Repeated(v) => {
            assert_eq!(v.len(), 2);
            assert!(matches!(v[0], bschema::Value::Sint32(-7)));
        }
        other => panic!("{other:?}"),
    }

    // Mixed on the same wire: one packed region then one unpacked entry.
    use bschema::varint::write_uvarint;
    use bschema::wire::{make_tag, TagId};
    let mut mixed = bin_packed.clone();
    // Append an unpacked sint32 entry: tag field4/zigzag, zigzag(4)=8.
    let mut tail = Vec::new();
    write_uvarint(&mut tail, make_tag(4, TagId::Zigzag)).unwrap();
    write_uvarint(&mut tail, 8u64).unwrap();
    // Must be inserted before any unknowns — there are none here, and known
    // fields are trailing only in canonical output; append is valid.
    mixed.extend_from_slice(&tail);
    let out3 = reader::decode(&packed, &mixed, &Limits::default()).unwrap();
    match out3.message.fields.get(&4).unwrap() {
        bschema::value::FieldValue::Repeated(v) => assert_eq!(v.len(), 4),
        other => panic!("{other:?}"),
    }
}

// ---------------------------------------------------------------------------
// Incompatible evolution
// ---------------------------------------------------------------------------

#[test]
fn incompatible_type_change_is_rejected() {
    let good = schema(PERSON_V1);
    let bad = schema(PERSON_BAD_TYPE);

    let bin = enc(&good, r#"{"id":1,"name":"n","email":"x@y.z"}"#);
    let err = reader::decode(&bad, &bin, &Limits::default()).unwrap_err();
    match err {
        bschema::Error::TypeMismatch {
            number,
            expected,
            found,
            ..
        } => {
            assert_eq!(number, 3);
            assert_eq!(expected, "varint");
            assert_eq!(found, "string");
        }
        other => panic!("expected TypeMismatch, got {other:?}"),
    }
}

#[test]
fn int_widening_always_works_narrowing_fails_on_value() {
    let v32 = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
        {"number":1,"name":"x","type":"int32","required":true}]}]}"#,
    );
    let v64 = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
        {"number":1,"name":"x","type":"int64","required":true}]}]}"#,
    );

    let bin = enc(&v32, r#"{"x":123}"#);
    assert!(reader::decode(&v64, &bin, &Limits::default()).is_ok());

    // Write a value an int32 reader cannot hold.
    let big = enc(&v64, r#"{"x":5000000000}"#);
    let err = reader::decode(&v32, &big, &Limits::default()).unwrap_err();
    assert!(matches!(err, bschema::Error::Wire(_)), "got {err:?}");
}

#[test]
fn negative_value_rejected_for_unsigned_varint_types() {
    let s = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
        {"number":1,"name":"x","type":"int64"}]}]}"#,
    );
    let m = value::json_to_message(&s, &parse(r#"{"x":-1}"#).unwrap());
    assert!(m.is_ok()); // JSON conversion accepts; encoder must reject.
    let bin = writer::encode(
        &s,
        &value::json_to_message(&s, &parse(r#"{"x":-1}"#).unwrap()).unwrap(),
        &Limits::default(),
    );
    assert!(bin.is_err(), "negative int64 must be rejected (use sint64)");
}

// ---------------------------------------------------------------------------
// Truncation
// ---------------------------------------------------------------------------

#[test]
fn truncation_at_every_cut_point_is_rejected_cleanly() {
    let s = schema(PERSON_V2);
    let bin = enc(
        &s,
        r#"{"id":42,"name":"ada","email":"a@b.c","nickname":"A",
            "tags":["x"],"scores":[-9,10],"address":{"street":"st","zip":7}}"#,
    );
    let n = bin.len();
    let mut boundary_successes = 0usize;
    for cut in 0..n {
        let prefix = &bin[..cut];
        match reader::decode(&s, prefix, &Limits::default()) {
            Ok(out) => {
                // A strict prefix can only succeed at a clean *entry
                // boundary* of the ROOT message (streaming framing), and
                // required fields (id/name) must then both be present.
                boundary_successes += 1;
                assert!(out.message.get(1).is_some(), "cut {cut}: id missing");
                assert!(out.message.get(2).is_some(), "cut {cut}: name missing");
            }
            Err(e) => {
                // Every other cut must be a structured error, never a panic.
                let msg = e.to_string();
                assert!(
                    matches!(e, bschema::Error::Wire(..))
                        || matches!(e, bschema::Error::Utf8(..))
                        || matches!(e, bschema::Error::MissingRequired { .. }),
                    "cut {cut}: unexpected error type: {msg}"
                );
            }
        }
    }
    // There must be at least one legal boundary prefix (e.g. everything
    // before the final entry), proving root framing is length-less.
    assert!(boundary_successes >= 1, "expected clean boundary prefixes");
    // And the complete message decodes.
    assert!(reader::decode(&s, &bin, &Limits::default()).is_ok());
}

#[test]
fn declared_length_beyond_stream_is_truncation() {
    // Hand-built: field 1 STRING, length 10, only 3 bytes follow.
    let mut bin = vec![b'B', b'S', b'E', b'1', 1u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    use bschema::varint::write_uvarint;
    use bschema::wire::{make_tag, TagId};
    write_uvarint(&mut bin, make_tag(1, TagId::String)).unwrap();
    write_uvarint(&mut bin, 10u64).unwrap();
    bin.extend_from_slice(b"abc");
    let s = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
        {"number":1,"name":"s","type":"string"}]}]}"#,
    );
    let err = reader::decode(&s, &bin, &Limits::default()).unwrap_err();
    assert!(err.to_string().contains("truncated"), "{err:?}");
}

// ---------------------------------------------------------------------------
// Wire validation
// ---------------------------------------------------------------------------

#[test]
fn noncanonical_bool_is_rejected() {
    let s = schema(
        r#"{"root":"R","messages":[{"name":"R","fields":[
        {"number":1,"name":"b","type":"bool"}]}]}"#,
    );
    let mut bin = vec![b'B', b'S', b'E', b'1', 1u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    use bschema::varint::write_uvarint;
    use bschema::wire::{make_tag, TagId};
    write_uvarint(&mut bin, make_tag(1, TagId::Bool)).unwrap();
    write_uvarint(&mut bin, 2u64).unwrap();
    let err = reader::decode(&s, &bin, &Limits::default()).unwrap_err();
    assert!(err.to_string().contains("non-canonical"), "{err:?}");
}

#[test]
fn unknown_tag_id_is_rejected_as_wire_error() {
    // Tag id 15 doesn't exist (field 1, id 0x0f => tag value 0x1f = 31).
    let mut bin = vec![b'B', b'S', b'E', b'1', 1u8, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    bin.push(0x1f);
    let s = schema(r#"{"root":"R","messages":[{"name":"R","fields":[]}]}"#);
    let err = reader::decode(&s, &bin, &Limits::default()).unwrap_err();
    assert!(matches!(err, bschema::Error::Wire(..)), "{err:?}");
}

#[test]
fn bad_magic_and_bad_version_rejected() {
    let s = schema(PERSON_V1);
    let bin = enc(&s, r#"{"id":1,"name":"n"}"#);
    let mut bad = bin.clone();
    bad[0] = b'X';
    assert!(matches!(
        reader::decode(&s, &bad, &Limits::default()).unwrap_err(),
        bschema::Error::Wire(..)
    ));
    let mut bad2 = bin;
    bad2[4] = 99;
    assert!(matches!(
        reader::decode(&s, &bad2, &Limits::default()).unwrap_err(),
        bschema::Error::Wire(..)
    ));
}

// ---------------------------------------------------------------------------
// Limits
// ---------------------------------------------------------------------------

#[test]
fn byte_limit_is_enforced() {
    let s = schema(PERSON_V2);
    let bin = enc(&s, r#"{"id":1,"name":"ada","tags":["aaaaaaaaaaaaaaaa"]}"#);
    let tight = Limits::default().with_max_message_bytes(10);
    let err = reader::decode(&s, &bin, &tight).unwrap_err();
    assert!(matches!(err, bschema::Error::LimitExceeded(..)), "{err:?}");
}

#[test]
fn output_limit_is_enforced() {
    let s = schema(PERSON_V2);
    let m = value::json_to_message(
        &s,
        &parse(r#"{"id":1,"name":"ada","tags":["aaaaaaaaaaaaaaaa"]}"#).unwrap(),
    )
    .unwrap();
    let tight = Limits::default().with_max_output_bytes(20);
    assert!(matches!(
        writer::encode(&s, &m, &tight).unwrap_err(),
        bschema::Error::LimitExceeded(..)
    ));
}

#[test]
fn unknown_field_bytes_count_toward_limit() {
    let s = schema(r#"{"root":"P","messages":[{"name":"P","fields":[]}]}"#);
    // Big unknown field via v2 schema with one long string.
    let v2 = schema(PERSON_V2);
    let big = "x".repeat(5000);
    let bin = enc(&v2, &format!(r#"{{"id":1,"name":"n","nickname":"{big}"}}"#));
    let tight = Limits::default().with_max_message_bytes(100);
    assert!(matches!(
        reader::decode(&s, &bin, &tight).unwrap_err(),
        bschema::Error::LimitExceeded(..)
    ));
}

#[test]
fn depth_limit_is_enforced() {
    // Chain of nested A -> A messages.
    let s = schema(
        r#"{"root":"A","messages":[{"name":"A","fields":[
           {"number":1,"name":"child","type":"A"}]}]}"#,
    );
    // Build depth 10 programmatically.
    let mut mv = MessageValue::new();
    for _ in 0..10 {
        let mut parent = MessageValue::new();
        parent.put(1, bschema::Value::Msg(mv));
        mv = parent;
    }
    let limits = Limits::default().with_max_nesting(5);
    let err = writer::encode(&s, &mv, &limits).unwrap_err();
    assert!(matches!(err, bschema::Error::LimitExceeded(..)), "{err:?}");
    // Decoder side too.
    let bin = writer::encode(&s, &mv, &Limits::default().with_max_nesting(64)).unwrap();
    assert!(matches!(
        reader::decode(&s, &bin, &limits).unwrap_err(),
        bschema::Error::LimitExceeded(..)
    ));
}

#[test]
fn repeated_count_limit_is_enforced() {
    let s = schema(PERSON_V2);
    let vals: Vec<String> = (0..100).map(|i| format!("\"v{i}\"")).collect();
    let bin = enc(
        &s,
        &format!(r#"{{"id":1,"name":"n","tags":[{}]}}"#, vals.join(",")),
    );
    let tight = Limits::default().with_max_repeated(10);
    assert!(matches!(
        reader::decode(&s, &bin, &tight).unwrap_err(),
        bschema::Error::LimitExceeded(..)
    ));
}

// ---------------------------------------------------------------------------
// Streaming
// ---------------------------------------------------------------------------

#[test]
fn decode_from_stream_matches_buffer_decode() {
    let s = schema(PERSON_V2);
    let bin = enc(&s, r#"{"id":3,"name":"n","nickname":"k","tags":["a"]}"#);
    let a = reader::decode(&s, &bin, &Limits::default()).unwrap();
    let b = reader::decode_from_stream(&s, std::io::Cursor::new(bin), &Limits::default()).unwrap();
    assert_eq!(a.message, b.message);
}

// ---------------------------------------------------------------------------
// JSON request envelope
// ---------------------------------------------------------------------------

#[test]
fn request_envelope_encode_decode_forward() {
    let schema_doc = parse(PERSON_V1).unwrap();
    let req = Json::Object(vec![
        ("op".into(), Json::Str("encode".into())),
        ("schema".into(), schema_doc.clone()),
        (
            "message".into(),
            parse(r#"{"id":11,"name":"ada","scores":[1,-2]}"#).unwrap(),
        ),
    ]);
    let resp = bschema::cli::handle_request(&req).unwrap();
    let b64 = resp
        .get("output_base64")
        .unwrap()
        .as_str()
        .unwrap()
        .to_string();
    assert!(resp.get("fingerprint").is_some());

    // Decode envelope.
    let req2 = Json::Object(vec![
        ("op".into(), Json::Str("decode".into())),
        ("schema".into(), schema_doc.clone()),
        ("input_base64".into(), Json::Str(b64.clone())),
    ]);
    let resp2 = bschema::cli::handle_request(&req2).unwrap();
    assert_eq!(resp2.get("fingerprint_match").unwrap(), &Json::Bool(true));
    let msg = resp2.get("message").unwrap();
    assert_eq!(msg.get("id").unwrap(), &Json::Int(11));
    assert_eq!(msg.get("name").unwrap(), &Json::Str("ada".into()));

    // Forward through V2 schema: unknown fields survive in the binary.
    let v2_doc = parse(PERSON_V2).unwrap();
    // First produce richer v2 data.
    let rich_req = Json::Object(vec![
        ("op".into(), Json::Str("encode".into())),
        ("schema".into(), v2_doc.clone()),
        (
            "message".into(),
            parse(r#"{"id":12,"name":"g","nickname":"grace","tags":["x"]}"#).unwrap(),
        ),
    ]);
    let rich_b64 = bschema::cli::handle_request(&rich_req)
        .unwrap()
        .get("output_base64")
        .unwrap()
        .as_str()
        .unwrap()
        .to_string();

    let fwd_req = Json::Object(vec![
        ("op".into(), Json::Str("forward".into())),
        ("schema".into(), schema_doc),
        ("input_base64".into(), Json::Str(rich_b64.clone())),
    ]);
    let fwd_resp = bschema::cli::handle_request(&fwd_req).unwrap();
    let fwd_b64 = fwd_resp.get("output_base64").unwrap().as_str().unwrap();

    let dec_req = Json::Object(vec![
        ("op".into(), Json::Str("decode".into())),
        ("schema".into(), v2_doc),
        ("input_base64".into(), Json::Str(fwd_b64.into())),
    ]);
    let dec = bschema::cli::handle_request(&dec_req).unwrap();
    let msg = dec.get("message").unwrap();
    assert_eq!(msg.get("nickname").unwrap(), &Json::Str("grace".into()));
}

#[test]
fn request_envelope_reports_errors_as_err_not_panic() {
    let req = parse(
        r#"{"op":"decode","schema":{
        "root":"P","messages":[{"name":"P","fields":[
          {"number":1,"name":"id","type":"int32","required":true}]}]},
        "input_base64":"QgNFMQEAAAAAAAAAAAAA"}"#,
    )
    .unwrap();
    // Body contains no fields: required id missing -> Err.
    let result = bschema::cli::handle_request(&req);
    assert!(result.is_err());
}
