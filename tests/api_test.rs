//! Tests for the JSON control entry and the base64/JSON helpers.

use rbitmap::api;

fn build_set(values: &[u32]) -> String {
    let req = format!(
        "{{\"op\":\"build\",\"values\":[{}]}}",
        values
            .iter()
            .map(u32::to_string)
            .collect::<Vec<_>>()
            .join(",")
    );
    let resp = api::handle(&req);
    assert!(resp.contains("\"ok\":true"), "build failed: {resp}");
    // Extract the "set" string field.
    extract_str(&resp, "set")
}

fn extract_str(json: &str, key: &str) -> String {
    let needle = format!("\"{key}\":\"");
    let start = json.find(&needle).expect("key present") + needle.len();
    let rest = &json[start..];
    let end = rest.find('"').expect("closing quote");
    rest[..end].to_string()
}

#[test]
fn build_and_decode_roundtrip() {
    let set = build_set(&[1, 2, 3, 70000, u32::MAX]);
    let resp = api::handle(&format!("{{\"op\":\"decode\",\"set\":\"{set}\"}}"));
    assert!(resp.contains("\"ok\":true"), "{resp}");
    assert!(
        resp.contains("\"values\":[1,2,3,70000,4294967295]"),
        "{resp}"
    );
}

#[test]
fn union_intersect_difference_via_json() {
    let a = build_set(&[1, 2, 3, 4]);
    let b = build_set(&[3, 4, 5]);

    let u = api::handle(&format!(
        "{{\"op\":\"union\",\"set\":\"{a}\",\"other\":\"{b}\"}}"
    ));
    assert!(u.contains("\"ok\":true"), "{u}");
    let uset = extract_str(&u, "set");
    let udec = api::handle(&format!("{{\"op\":\"decode\",\"set\":\"{uset}\"}}"));
    assert!(udec.contains("\"values\":[1,2,3,4,5]"), "{udec}");

    let i = api::handle(&format!(
        "{{\"op\":\"intersect\",\"set\":\"{a}\",\"other\":\"{b}\"}}"
    ));
    let iset = extract_str(&i, "set");
    let idec = api::handle(&format!("{{\"op\":\"decode\",\"set\":\"{iset}\"}}"));
    assert!(idec.contains("\"values\":[3,4]"), "{idec}");

    let d = api::handle(&format!(
        "{{\"op\":\"difference\",\"set\":\"{a}\",\"other\":\"{b}\"}}"
    ));
    let dset = extract_str(&d, "set");
    let ddec = api::handle(&format!("{{\"op\":\"decode\",\"set\":\"{dset}\"}}"));
    assert!(ddec.contains("\"values\":[1,2]"), "{ddec}");
}

#[test]
fn insert_remove_contains_via_json() {
    let mut set = build_set(&[10, 20]);
    let r = api::handle(&format!(
        "{{\"op\":\"insert\",\"set\":\"{set}\",\"value\":30}}"
    ));
    assert!(r.contains("\"changed\":true"), "{r}");
    set = extract_str(&r, "set");

    let r = api::handle(&format!(
        "{{\"op\":\"contains\",\"set\":\"{set}\",\"value\":30}}"
    ));
    assert!(r.contains("\"present\":true"), "{r}");

    let r = api::handle(&format!(
        "{{\"op\":\"remove\",\"set\":\"{set}\",\"value\":30}}"
    ));
    assert!(r.contains("\"changed\":true"), "{r}");
    let r2 = api::handle(&format!(
        "{{\"op\":\"contains\",\"set\":\"{}\",\"value\":30}}",
        extract_str(&r, "set")
    ));
    assert!(r2.contains("\"present\":false"), "{r2}");
}

#[test]
fn stats_reports_container_kinds() {
    // 5000 values in one key forces a bitmap container.
    let values: Vec<u32> = (0..5000).collect();
    let set = build_set(&values);
    let resp = api::handle(&format!("{{\"op\":\"stats\",\"set\":\"{set}\"}}"));
    assert!(resp.contains("\"bitmap_containers\":1"), "{resp}");
    assert!(resp.contains("\"cardinality\":5000"), "{resp}");
}

#[test]
fn malformed_requests_return_ok_false_not_panic() {
    for bad in [
        "",
        "not json",
        "{}",
        "{\"op\":\"nope\"}",
        "{\"op\":\"build\"}",
        "{\"op\":\"build\",\"values\":[1,2,\"x\"]}",
        "{\"op\":\"build\",\"values\":[-1]}",
        "{\"op\":\"build\",\"values\":[4294967296]}",
        "{\"op\":\"decode\",\"set\":\"!!!not-base64!!!\"}",
        "{\"op\":\"decode\",\"set\":\"AAAA\"}", // valid b64, invalid RBM1
        "{\"op\":\"union\",\"set\":\"AAAA\"}",  // missing other
    ] {
        let resp = api::handle(bad);
        assert!(resp.contains("\"ok\":false"), "input {bad:?} -> {resp}");
    }
}

#[test]
fn decode_output_limit_enforced() {
    let values: Vec<u32> = (0..100).collect();
    let set = build_set(&values);
    let resp = api::handle(&format!(
        "{{\"op\":\"decode\",\"set\":\"{set}\",\"limits\":{{\"max_output\":10}}}}"
    ));
    assert!(resp.contains("\"ok\":false"), "{resp}");
    assert!(resp.contains("exceeds limit"), "{resp}");
}

#[test]
fn decode_input_limits_enforced() {
    let values: Vec<u32> = (0..100).collect();
    let set = build_set(&values);
    // max_values below the set's cardinality must reject at decode time.
    let resp = api::handle(&format!(
        "{{\"op\":\"stats\",\"set\":\"{set}\",\"limits\":{{\"max_values\":10}}}}"
    ));
    assert!(resp.contains("\"ok\":false"), "{resp}");
}

#[test]
fn json_string_escapes_and_unicode() {
    // Exercise the hand-rolled parser via a hostile op string.
    let resp = api::handle("{\"op\":\"\\u006e\\u006f\\u0070\\u0065\"}");
    assert!(resp.contains("\"ok\":false"), "{resp}");
    assert!(resp.contains("unknown op"), "{resp}");
}
