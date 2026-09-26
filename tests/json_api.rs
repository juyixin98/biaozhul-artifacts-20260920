//! End-to-end tests for the JSON control surface (the same dispatch the
//! `rbitset` binary uses).

use rbitset::json::{base64_decode, base64_encode, dispatch, parse, stringify};
use rbitset::set::IntSet;

fn run(request: &str) -> String {
    // Mirror the binary: a parse failure is itself an ok:false response.
    let req = match parse(request) {
        Ok(v) => v,
        Err(e) => {
            let mut m = std::collections::BTreeMap::new();
            m.insert("ok".to_string(), rbitset::json::Json::Bool(false));
            m.insert("error".to_string(), rbitset::json::Json::Str(e.to_string()));
            return stringify(&rbitset::json::Json::Obj(m));
        }
    };
    stringify(&dispatch(&req))
}

#[test]
fn encode_then_decode_round_trip() {
    let resp = run(r#"{"op":"encode","set":{"values":[1,2,42,4294967295,2]}}"#);
    assert!(resp.contains("\"ok\":true"), "resp={resp}");
    let v = parse(&resp).unwrap();
    let obj = match &v {
        rbitset::json::Json::Obj(o) => o,
        _ => panic!(),
    };
    let data = match obj.get("data").unwrap() {
        rbitset::json::Json::Str(s) => s.clone(),
        _ => panic!(),
    };
    let decoded = run(&format!(r#"{{"op":"decode","data":"{data}"}}"#));
    assert!(decoded.contains("\"cardinality\":4"), "decoded={decoded}");
    assert!(decoded.contains("4294967295"));
}

#[test]
fn union_intersection_difference_via_json() {
    let req = r#"{"op":"union","a":{"values":[1,2,3]},"b":{"values":[3,4,5]}}"#;
    let resp = run(req);
    assert!(resp.contains("\"cardinality\":5"), "resp={resp}");

    let resp = run(r#"{"op":"intersection","a":{"values":[1,2,3]},"b":{"values":[3,4,5]}}"#);
    assert!(resp.contains("\"cardinality\":1"), "resp={resp}");

    let resp = run(r#"{"op":"difference","a":{"values":[1,2,3]},"b":{"values":[3,4,5]}}"#);
    assert!(resp.contains("\"cardinality\":2"), "resp={resp}");
}

#[test]
fn binary_input_through_base64_field() {
    // Encode natively, then feed the base64 payload as a set operand.
    let s = IntSet::from_values(&[7, 8, 9]);
    let raw = rbitset::codec::encode_to_vec(&s).unwrap();
    let b64 = base64_encode(&raw);
    let req = format!(r#"{{"op":"contains","set":{{"data":"{b64}"}},"value":8}}"#);
    let resp = run(&req);
    assert!(resp.contains("\"contains\":true"), "resp={resp}");
    let req = format!(r#"{{"op":"contains","set":{{"data":"{b64}"}},"value":10}}"#);
    let resp = run(&req);
    assert!(resp.contains("\"contains\":false"), "resp={resp}");
}

#[test]
fn malformed_requests_report_ok_false() {
    assert!(run("not json").contains("\"ok\":false"));
    assert!(run("{}").contains("missing"));
    assert!(run(r#"{"op":"frobnicate"}"#).contains("unknown op"));
    assert!(run(r#"{"op":"encode","set":{"values":["x"]}}"#).contains("must be a non-negative"));
    assert!(run(r#"{"op":"decode","data":"@@@"}"#).contains("base64"));
}

#[test]
fn value_limit_enforced_through_json() {
    let s = IntSet::from_values(&(0..100u32).collect::<Vec<_>>());
    let raw = rbitset::codec::encode_to_vec(&s).unwrap();
    let b64 = base64_encode(&raw);
    let req = format!(r#"{{"op":"decode","data":"{b64}","max_values":10}}"#);
    let resp = run(&req);
    assert!(
        resp.contains("\"ok\":false") && resp.contains("limit"),
        "resp={resp}"
    );
}

#[test]
fn base64_known_vectors() {
    assert_eq!(base64_encode(b""), "");
    assert_eq!(base64_encode(b"f"), "Zg==");
    assert_eq!(base64_encode(b"fo"), "Zm8=");
    assert_eq!(base64_encode(b"foo"), "Zm9v");
    assert_eq!(base64_decode("Zm9v").unwrap(), b"foo");
    assert!(base64_decode("Zg=").is_err()); // bad padding length
    assert!(base64_decode("!!!!").is_err());
}

#[test]
fn info_reports_container_kinds() {
    // > 4096 in one chunk forces a bitmap container.
    let vals: Vec<u32> = (0..5000u32).collect();
    let raw = rbitset::codec::encode_to_vec(&IntSet::from_values(&vals)).unwrap();
    let b64 = base64_encode(&raw);
    let resp = run(&format!(r#"{{"op":"info","data":"{b64}"}}"#));
    assert!(resp.contains("\"kind\":\"bitmap\""), "resp={resp}");
    assert!(resp.contains("\"cardinality\":5000"), "resp={resp}");
}
