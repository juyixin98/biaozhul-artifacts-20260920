//! JSON 控制入口测试。

use canonical_huffman::jsonapi::{handle_request, hex_encode};

#[test]
fn json_compress_decompress_base64_roundtrip() {
    let input = b"the quick brown fox jumps over the lazy dog. ".repeat(50);
    let req = format!(
        "{{\"op\":\"compress\",\"data\":\"{}\"}}",
        canonical_huffman::jsonapi::base64_encode(&input)
    );
    let resp = handle_request(&req);
    let v = parse(&resp);
    assert!(get_bool(&v, "ok"), "resp={resp}");
    let compressed = get_str(&v, &["result", "data"]).to_string();

    let req2 = format!("{{\"op\":\"decompress\",\"data\":\"{compressed}\"}}");
    let resp2 = handle_request(&req2);
    let v2 = parse(&resp2);
    assert!(get_bool(&v2, "ok"), "resp2={resp2}");
    let out = canonical_huffman::jsonapi::base64_decode(get_str(&v2, &["result", "data"])).unwrap();
    assert_eq!(out, input);
}

#[test]
fn json_hex_roundtrip_empty() {
    let resp = handle_request("{\"op\":\"compress\",\"data\":\"\",\"encoding\":\"hex\"}");
    let v = parse(&resp);
    assert!(get_bool(&v, "ok"));
    // 空输入压缩结果 = 前奏 6 字节 + 块头 19 字节。
    assert_eq!(
        get_str(&v, &["result", "data"]),
        hex_encode(&{
            let mut b = b"CHFC".to_vec();
            b.push(1);
            b.push(0);
            b.push(0x01);
            b.extend_from_slice(&[0u8; 18]);
            b
        })
    );
    let compressed = get_str(&v, &["result", "data"]).to_string();
    let resp2 = handle_request(&format!(
        "{{\"op\":\"decompress\",\"data\":\"{compressed}\",\"encoding\":\"hex\"}}"
    ));
    let v2 = parse(&resp2);
    assert!(get_bool(&v2, "ok"), "resp2={resp2}");
    assert_eq!(get_str(&v2, &["result", "data"]), "");
}

#[test]
fn json_error_envelope_on_bad_magic() {
    // 6 个 0xFF 字节：足够长到进入魔数校验，但魔数错误。
    let resp =
        handle_request("{\"op\":\"decompress\",\"data\":\"////////\",\"encoding\":\"base64\"}");
    let v = parse(&resp);
    assert!(!get_bool(&v, "ok"));
    assert_eq!(get_str(&v, &["error", "kind"]), "bad_magic");
}

#[test]
fn json_error_on_missing_op() {
    let resp = handle_request("{\"data\":\"\"}");
    let v = parse(&resp);
    assert!(!get_bool(&v, "ok"));
    assert_eq!(get_str(&v, &["error", "kind"]), "bad_request");
}

#[test]
fn json_error_on_bad_base64() {
    let resp = handle_request("{\"op\":\"decompress\",\"data\":\"@@@\"}");
    let v = parse(&resp);
    assert!(!get_bool(&v, "ok"));
    assert_eq!(get_str(&v, &["error", "kind"]), "bad_request");
}

#[test]
fn json_output_limit_enforced() {
    // 先压缩 1000 个同字节。
    let input = vec![0x41u8; 1000];
    let resp = handle_request(&format!(
        "{{\"op\":\"compress\",\"data\":\"{}\"}}",
        canonical_huffman::jsonapi::base64_encode(&input)
    ));
    let v = parse(&resp);
    let compressed = get_str(&v, &["result", "data"]).to_string();

    let resp2 = handle_request(&format!(
        "{{\"op\":\"decompress\",\"data\":\"{compressed}\",\"max_block_bytes\":500}}"
    ));
    let v2 = parse(&resp2);
    assert!(!get_bool(&v2, "ok"), "resp2={resp2}");
    assert_eq!(get_str(&v2, &["error", "kind"]), "limit_exceeded");
}

#[test]
fn json_compress_reports_ratio() {
    let input = vec![b'a'; 32];
    let b64 = canonical_huffman::jsonapi::base64_encode(&input);
    let resp = handle_request(&format!("{{\"op\":\"compress\",\"data\":\"{b64}\"}}"));
    let v = parse(&resp);
    assert!(get_bool(&v, "ok"));
    let ratio = get_num(&v, &["result", "stats", "ratio"]);
    assert!(ratio > 0.0 && ratio < 1.0, "ratio={ratio}");
}

// ---- 极简 JSON 导航辅助（直接复用库内解析器） ----

fn parse(s: &str) -> canonical_huffman::jsonapi::JsonValue {
    canonical_huffman::jsonapi::parse_json(s).unwrap()
}

fn navigate<'a>(
    v: &'a canonical_huffman::jsonapi::JsonValue,
    path: &[&str],
) -> &'a canonical_huffman::jsonapi::JsonValue {
    let mut cur = v;
    for key in path {
        cur = cur
            .get(key)
            .unwrap_or_else(|| panic!("missing key {key} in {path:?}"));
    }
    cur
}

fn get_bool(v: &canonical_huffman::jsonapi::JsonValue, key: &str) -> bool {
    matches!(v.get(key), Some(canonical_huffman::jsonapi::JsonValue::Bool(b)) if *b)
}

fn get_str<'a>(v: &'a canonical_huffman::jsonapi::JsonValue, path: &[&str]) -> &'a str {
    match navigate(v, path) {
        canonical_huffman::jsonapi::JsonValue::Str(s) => s,
        other => panic!("expected string at {path:?}, got {other:?}"),
    }
}

fn get_num(v: &canonical_huffman::jsonapi::JsonValue, path: &[&str]) -> f64 {
    match navigate(v, path) {
        canonical_huffman::jsonapi::JsonValue::Num(f, _) => *f,
        other => panic!("expected number at {path:?}, got {other:?}"),
    }
}
