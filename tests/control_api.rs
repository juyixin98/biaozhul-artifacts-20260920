//! JSON 控制入口测试：encode/decode/merge/inspect 全链路（含 base64 承载）。

use cdc::control::{handle, handle_pretty};
use cdc::json;

/// 取响应中的 base64 数据字段并解码。
fn data_field(resp: &str, field: &str) -> Vec<u8> {
    let v = json::parse(resp).unwrap();
    let b64 = v.get(field).and_then(|x| x.as_str()).unwrap();
    cdc::base64::decode(b64).unwrap()
}

#[test]
fn encode_decode_via_json() {
    let req = r#"{
        "op": "encode",
        "rows": ["alpha", null, "", "beta", "alpha", "你好"]
    }"#;
    let resp = handle(req);
    let v = json::parse(&resp).unwrap();
    assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(true));
    assert_eq!(v.get("write_stats.rows").and_then(|x| x.as_u64()), Some(6));
    assert_eq!(v.get("write_stats.nulls").and_then(|x| x.as_u64()), Some(1));

    let data = data_field(&resp, "data_b64");

    let dec = handle(&format!(
        r#"{{"op":"decode","data_b64":"{}"}}"#,
        cdc::base64::encode(&data)
    ));
    let dv = json::parse(&dec).unwrap();
    let rows = dv.get("rows").unwrap().as_array().unwrap();
    assert_eq!(rows[0].as_str(), Some("alpha"));
    assert_eq!(rows[1], json::Value::Null);
    assert_eq!(rows[2].as_str(), Some(""));
    assert_eq!(rows[5].as_str(), Some("你好"));
}

#[test]
fn encode_with_explicit_segments_then_inspect() {
    let req = r#"{
        "op": "encode",
        "segments": [["b","a"], [null,"a"], ["c"]]
    }"#;
    let resp = handle(req);
    let data = data_field(&resp, "data_b64");

    let inspect = handle(&format!(
        r#"{{"op":"inspect","data_b64":"{}"}}"#,
        cdc::base64::encode(&data)
    ));
    let iv = json::parse(&inspect).unwrap();
    let segs = iv.get("segments").unwrap().as_array().unwrap();
    assert_eq!(segs.len(), 3);
    assert_eq!(segs[0].get("cardinality").and_then(|x| x.as_u64()), Some(2));
    assert_eq!(segs[1].get("nulls").and_then(|x| x.as_u64()), Some(1));
}

#[test]
fn merge_via_json_matches_concatenation() {
    // 两个文件，字典里各自 ID 含义不同，相同字节跨段重复。
    let r1 = handle(r#"{"op":"encode","segments":[["b","a","b"]]}"#);
    let r2 = handle(r#"{"op":"encode","segments":[["a","c"],[null,"b"]]}"#);
    let d1 = data_field(&r1, "data_b64");
    let d2 = data_field(&r2, "data_b64");

    let merge_req = format!(
        r#"{{"op":"merge","canonical":true,"inputs_b64":["{}","{}"]}}"#,
        cdc::base64::encode(&d1),
        cdc::base64::encode(&d2)
    );
    let resp = handle(&merge_req);
    let mv = json::parse(&resp).unwrap();
    assert_eq!(mv.get("ok").and_then(|x| x.as_bool()), Some(true));
    assert_eq!(
        mv.get("merge_stats.global_distinct")
            .and_then(|x| x.as_u64()),
        Some(3) // a,b,c（NULL 不计数，空串未出现）
    );
    assert!(
        mv.get("merge_stats.deduped")
            .and_then(|x| x.as_u64())
            .unwrap()
            >= 1
    );

    let merged = data_field(&resp, "data_b64");
    let dec = handle(&format!(
        r#"{{"op":"decode","data_b64":"{}"}}"#,
        cdc::base64::encode(&merged)
    ));
    let decoded = json::parse(&dec).unwrap();
    let rows = decoded.get("rows").unwrap().as_array().unwrap();
    // 拼接顺序：b,a,b, a,c, null,b
    let expected: Vec<Option<&str>> = vec![
        Some("b"),
        Some("a"),
        Some("b"),
        Some("a"),
        Some("c"),
        None,
        Some("b"),
    ];
    assert_eq!(rows.len(), expected.len());
    for (i, e) in expected.iter().enumerate() {
        match e {
            None => assert_eq!(rows[i], json::Value::Null),
            Some(s) => assert_eq!(rows[i].as_str(), Some(*s)),
        }
    }
}

#[test]
fn errors_are_structured_not_panics() {
    // 非法 JSON
    let r = handle("{not json");
    let v = json::parse(&r).unwrap();
    assert_eq!(v.get("ok").and_then(|x| x.as_bool()), Some(false));
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("bad_json")
    );

    // 未知 op
    let r = handle(r#"{"op":"frobnicate"}"#);
    let v = json::parse(&r).unwrap();
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("bad_request")
    );

    // 坏魔数（4 个零字节，长度足够但魔数不对）
    let r = handle(r#"{"op":"decode","data_b64":"AAAAAA=="}"#);
    let v = json::parse(&r).unwrap();
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("corrupt_or_unsupported_format")
    );

    // 截断（只有 3 字节，连魔数都读不满）
    let r = handle(r#"{"op":"decode","data_b64":"AAAA"}"#);
    let v = json::parse(&r).unwrap();
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("truncated_input")
    );

    // 坏 base64
    let r = handle(r#"{"op":"decode","data_b64":"@@@"}"#);
    let v = json::parse(&r).unwrap();
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("bad_base64")
    );
}

#[test]
fn limit_in_json_request_is_honored() {
    let req = r#"{
        "op": "decode",
        "data_b64": "PLACEHOLDER",
        "limits": { "max_rows": 1 }
    }"#;
    // 先编码 3 行。
    let enc = handle(r#"{"op":"encode","rows":["a","b","c"]}"#);
    let data = data_field(&enc, "data_b64");
    let req = req.replace("PLACEHOLDER", &cdc::base64::encode(&data));
    let resp = handle(&req);
    let v = json::parse(&resp).unwrap();
    assert_eq!(
        v.get("error.code").and_then(|x| x.as_str()),
        Some("limit_exceeded")
    );
}

#[test]
fn pretty_output_is_valid_json_and_pretty() {
    let resp = handle_pretty(r#"{"op":"encode","rows":["a"]}"#);
    assert!(resp.contains('\n'));
    assert!(json::parse(&resp).is_ok());
}
