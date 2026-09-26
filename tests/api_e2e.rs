//! End-to-end tests through the JSON control entry point (`api::execute`).

use bse::api;
use bse::json::{parse, to_string, JsonValue};

fn run(json: &str) -> JsonValue {
    let req = parse(json).expect("valid request JSON");
    api::execute(&req).expect("op should succeed")
}

fn run_err(json: &str) -> bse::Error {
    let req = parse(json).expect("valid request JSON");
    api::execute(&req).expect_err("op should fail")
}

const SCHEMA: &str = r#"{
  "root": "Person",
  "types": [{
    "name": "Person",
    "fields": [
      {"name":"id","number":1,"type":"int64","cardinality":"required"},
      {"name":"name","number":2,"type":"string","cardinality":"required"},
      {"name":"email","number":3,"type":"string","cardinality":"optional"},
      {"name":"tags","number":4,"type":"string","cardinality":"repeated"}
    ]
  }]
}"#;

fn result_of(resp: &JsonValue) -> &JsonValue {
    resp.as_object().unwrap().get("result").unwrap()
}

#[test]
fn encode_then_decode_round_trip_via_json() {
    let enc_req = format!(
        r#"{{"op":"encode","schema":{schema},"message":{{"id":0,"name":"Ada","tags":["x"]}}}}"#,
        schema = SCHEMA
    );
    let enc = run(&enc_req);
    let b64 = result_of(&enc)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap()
        .to_string();

    let dec_req = format!(
        r#"{{"op":"decode","schema":{schema},"data_b64":"{b64}"}}"#,
        schema = SCHEMA,
        b64 = b64
    );
    let dec = run(&dec_req);
    let text = to_string(&dec).unwrap();
    // Explicit id 0 and present false-style zero preserved; email missing.
    assert!(text.contains("\"id\":{\"present\":0}"), "got {text}");
    assert!(text.contains("\"email\":{\"missing\":true}"));
    assert!(text.contains("\"tags\":{\"values\":[\"x\"]}"));
}

#[test]
fn forward_preserves_unknown_fields_and_applies_patch() {
    // New-schema data (has age field 7) produced directly as BSE1 bytes.
    let producer_schema = r#"{
      "root": "Person",
      "types": [{
        "name": "Person",
        "fields": [
          {"name":"id","number":1,"type":"int64","cardinality":"required"},
          {"name":"name","number":2,"type":"string","cardinality":"required"},
          {"name":"age","number":7,"type":"int32","cardinality":"optional"}
        ]
      }]
    }"#;
    let enc = run(&format!(
        r#"{{"op":"encode","schema":{s},"message":{{"id":1,"name":"A","age":30}}}}"#,
        s = producer_schema
    ));
    let b64 = result_of(&enc)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap();

    // Forward under the OLDER schema (no age): patch name, keep unknown.
    let fwd_req = format!(
        r#"{{"op":"forward","schema":{schema},"data_b64":"{b64}","set":{{"name":"B"}}}}"#,
        schema = SCHEMA,
        b64 = b64
    );
    let fwd = run(&fwd_req);
    let fobj = result_of(&fwd).as_object().unwrap();
    // One unknown field (age) was carried through.
    let stats = fobj.get("stats").unwrap().as_object().unwrap();
    assert_eq!(stats.get("unknown_fields").unwrap(), &JsonValue::Int(1));
    assert!(fobj.get("data_b64").is_some());
}

#[test]
fn compat_flags_incompatible_type_change() {
    let new_schema = r#"{
      "root": "Person",
      "types": [{
        "name": "Person",
        "fields": [
          {"name":"id","number":1,"type":"int64","cardinality":"required"},
          {"name":"name","number":2,"type":"string","cardinality":"required"},
          {"name":"email","number":3,"type":"int32","cardinality":"optional"}
        ]
      }]
    }"#;
    let resp = run(&format!(
        r#"{{"op":"compat","old_schema":{o},"new_schema":{n}}}"#,
        o = SCHEMA,
        n = new_schema
    ));
    let compatible = result_of(&resp)
        .as_object()
        .unwrap()
        .get("compatible")
        .unwrap();
    assert_eq!(compatible, &JsonValue::Bool(false));
}

#[test]
fn truncate_then_decode_reports_clean_error() {
    let enc = run(&format!(
        r#"{{"op":"encode","schema":{s},"message":{{"id":1,"name":"a long enough name here"}}}}"#,
        s = SCHEMA
    ));
    let b64 = result_of(&enc)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap();

    let cut = run(&format!(
        r#"{{"op":"truncate","data_b64":"{b64}","keep":3}}"#,
        b64 = b64
    ));
    let cut_b64 = result_of(&cut)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap();

    let err = run_err(&format!(
        r#"{{"op":"decode","schema":{s},"data_b64":"{b64}"}}"#,
        s = SCHEMA,
        b64 = cut_b64
    ));
    assert!(
        matches!(err, bse::Error::UnexpectedEof { .. }),
        "got {err:?}"
    );
}

#[test]
fn missing_required_is_a_structured_error() {
    let err = run_err(&format!(
        r#"{{"op":"encode","schema":{s},"message":{{"name":"no id"}}}}"#,
        s = SCHEMA
    ));
    assert!(matches!(err, bse::Error::MissingField(_)), "got {err:?}");
}

#[test]
fn unknown_op_and_bad_json_are_rejected() {
    let err = run_err(&format!(
        r#"{{"op":"frobnicate","schema":{s}}}"#,
        s = SCHEMA
    ));
    assert!(matches!(err, bse::Error::InvalidInput(_)));

    let req = parse(r#"{ not json"#).expect_err("parser rejects");
    assert!(matches!(req, bse::Error::Json(_)));
}

#[test]
fn explicit_zero_wrapper_is_not_missing() {
    let enc = run(&format!(
        r#"{{"op":"encode","schema":{s},"message":{{"id":{{"present":0}},"name":{{"present":"z"}},"email":{{"missing":true}}}}}}"#,
        s = SCHEMA
    ));
    let b64 = result_of(&enc)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap();
    let dec = run(&format!(
        r#"{{"op":"decode","schema":{s},"data_b64":"{b64}"}}"#,
        s = SCHEMA,
        b64 = b64
    ));
    let text = to_string(&dec).unwrap();
    assert!(text.contains("\"id\":{\"present\":0}"));
    assert!(text.contains("\"email\":{\"missing\":true}"));
}

#[test]
fn bytes_field_round_trips_base64() {
    let s = r#"{
      "root":"M",
      "types":[{"name":"M","fields":[
        {"name":"blob","number":1,"type":"bytes","cardinality":"optional"}
      ]}]
    }"#;
    // "foo" -> Zm9v
    let enc = run(&format!(
        r#"{{"op":"encode","schema":{s},"message":{{"blob":"Zm9v"}}}}"#,
        s = s
    ));
    let b64 = result_of(&enc)
        .as_object()
        .unwrap()
        .get("data_b64")
        .unwrap()
        .as_str()
        .unwrap();
    let dec = run(&format!(
        r#"{{"op":"decode","schema":{s},"data_b64":"{b64}"}}"#,
        s = s,
        b64 = b64
    ));
    assert!(to_string(&dec)
        .unwrap()
        .contains("\"blob\":{\"present\":\"Zm9v\"}"));
}
