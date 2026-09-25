//! 端到端集成测试：驱动 [`ipfrag::server::handle_request`] 协议层，
//! 覆盖任务验收要求的全部场景：
//! 乱序、重复片、重叠、缺首片、ID 重用、超时清理，并对照完整原始载荷。
//!
//! 这里不开真实 TCP 套接字（socket 行为见 tcp_server.rs），
//! 以便对时间相关路径做完全确定性的控制。

use ipfrag::json::{self, b, i, obj, s};
use ipfrag::reassembly::{OverlapPolicy, ReassemblerConfig};
use ipfrag::server::{handle_request, ServerState};
use ipfrag::sha256_hex;

fn test_config() -> ReassemblerConfig {
    ReassemblerConfig {
        assembly_ttl_ms: 500,
        purge_interval_ms: u64::MAX, // 协议测试中禁用机会式清理，时间由请求显式驱动
        mem_budget_bytes: 4096,
        max_datagram_bytes: 1024,
        max_assemblies: 64,
        overlap_policy: OverlapPolicy::RejectNewFragment,
    }
}

fn frag_req(id: i64, offset: i64, mf: bool, payload: &[u8]) -> json::JsonValue {
    obj(vec![
        ("op", s("frag")),
        ("src", s("10.1.0.1")),
        ("dst", s("10.1.0.2")),
        ("protocol", i(17)),
        ("id", i(id)),
        ("offset", i(offset)),
        ("mf", b(mf)),
        ("payload_hex", s(hex::encode(payload))),
    ])
}

fn frag_req_at(id: i64, offset: i64, mf: bool, payload: &[u8], now_ms: i64) -> json::JsonValue {
    let mut v = frag_req(id, offset, mf, payload);
    if let json::JsonValue::Object(map) = &mut v {
        map.insert("now_ms".to_string(), i(now_ms));
    }
    v
}

mod hex {
    pub fn encode(data: &[u8]) -> String {
        let mut s = String::with_capacity(data.len() * 2);
        for b in data {
            s.push_str(&format!("{b:02x}"));
        }
        s
    }
}

fn ok(resp: &json::JsonValue) {
    assert_eq!(
        resp.get("ok").and_then(|v| v.as_bool()),
        Some(true),
        "response not ok: {}",
        json::to_string(resp)
    );
}

fn event_of(resp: &json::JsonValue) -> &str {
    resp.get("event").and_then(|v| v.as_str()).unwrap_or("")
}

/// 把长度 n 的载荷按 mtu 切成 (offset, data, mf)。
fn split(payload: &[u8], mtu: usize) -> Vec<(usize, Vec<u8>, bool)> {
    assert!(mtu.is_multiple_of(8));
    let mut out = Vec::new();
    let mut off = 0;
    while off < payload.len() {
        let take = mtu.min(payload.len() - off);
        let last = off + take == payload.len();
        out.push((off, payload[off..off + take].to_vec(), !last));
        off += take;
    }
    out
}

#[test]
fn scenario_out_of_order_completes_and_matches_original_payload() {
    let mut st = ServerState::new(test_config());
    let payload: Vec<u8> = (0..200u32)
        .map(|i| (i.wrapping_mul(31) & 0xff) as u8)
        .collect();
    let pieces = split(&payload, 24);

    // 乱序：先末片，后中间，首片最后。
    let mut order: Vec<usize> = (0..pieces.len()).collect();
    order.reverse();

    let mut completed = None;
    for idx in order {
        let (off, data, mf) = &pieces[idx];
        let resp = handle_request(&mut st, &frag_req(1, *off as i64, *mf, data), 0);
        ok(&resp);
        if event_of(&resp) == "completed" {
            completed = Some(resp);
        }
    }
    let resp = completed.expect("datagram should complete once first fragment arrived");
    assert_eq!(resp.get("total_bytes").and_then(|v| v.as_i64()), Some(200));
    assert_eq!(
        resp.get("sha256").and_then(|v| v.as_str()),
        Some(sha256_hex(&payload).as_str())
    );
    assert_eq!(
        resp.get("fragment_count").and_then(|v| v.as_i64()),
        Some(pieces.len() as i64)
    );
}

#[test]
fn scenario_duplicate_fragment_is_idempotent() {
    let mut st = ServerState::new(test_config());
    let p = vec![7u8; 24];
    ok(&handle_request(&mut st, &frag_req(2, 0, true, &p), 0));

    // 完全重复 -> duplicate 事件
    let resp = handle_request(&mut st, &frag_req(2, 0, true, &p), 10);
    ok(&resp);
    assert_eq!(event_of(&resp), "duplicate");

    // 末片完成后，stats 中 duplicates=1，完成数=1
    let last = vec![9u8; 8];
    let resp = handle_request(&mut st, &frag_req(2, 24, false, &last), 10);
    assert_eq!(event_of(&resp), "completed");

    let stats = handle_request(&mut st, &obj(vec![("op", s("stats"))]), 10);
    assert_eq!(
        stats
            .get("stats")
            .unwrap()
            .get("duplicates")
            .unwrap()
            .as_i64(),
        Some(1)
    );
    assert_eq!(
        stats
            .get("stats")
            .unwrap()
            .get("datagrams_completed")
            .unwrap()
            .as_i64(),
        Some(1)
    );
}

#[test]
fn scenario_overlapping_fragment_is_rejected_with_error_type() {
    let mut st = ServerState::new(test_config());
    ok(&handle_request(
        &mut st,
        &frag_req(3, 0, true, &[1u8; 24]),
        0,
    ));

    // [8,32) 与已有 [0,24) 相交 -> 明确错误类型 overlap
    let resp = handle_request(&mut st, &frag_req(3, 8, true, &[2u8; 24]), 0);
    assert_eq!(resp.get("ok").and_then(|v| v.as_bool()), Some(false));
    assert_eq!(resp.get("error").and_then(|v| v.as_str()), Some("overlap"));
    let msg = resp.get("message").and_then(|v| v.as_str()).unwrap();
    assert!(
        msg.contains("overlapping fragment rejected"),
        "message was: {msg}"
    );

    // 原组装保留：status 仍显示 1 个片、24 字节
    let status = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("status")),
            ("src", s("10.1.0.1")),
            ("dst", s("10.1.0.2")),
            ("protocol", i(17)),
            ("id", i(3)),
        ]),
        0,
    );
    ok(&status);
    let assemblies = status.get("assemblies").unwrap().as_array().unwrap();
    assert_eq!(assemblies.len(), 1);
    assert_eq!(
        assemblies[0].get("fragment_count").unwrap().as_i64(),
        Some(1)
    );
    assert_eq!(
        assemblies[0].get("buffered_bytes").unwrap().as_i64(),
        Some(24)
    );
}

#[test]
fn scenario_missing_first_fragment_stays_pending_then_completes() {
    let mut st = ServerState::new(test_config());
    // 末片与中间片先到
    ok(&handle_request(
        &mut st,
        &frag_req(4, 24, false, &[3u8; 8]),
        0,
    ));
    ok(&handle_request(
        &mut st,
        &frag_req(4, 8, true, &[2u8; 16]),
        0,
    ));

    let status = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("status")),
            ("src", s("10.1.0.1")),
            ("dst", s("10.1.0.2")),
            ("protocol", i(17)),
            ("id", i(4)),
        ]),
        0,
    );
    let a = &status.get("assemblies").unwrap().as_array().unwrap()[0];
    assert_eq!(a.get("has_first_fragment").unwrap().as_bool(), Some(false));
    assert_eq!(a.get("has_last_fragment").unwrap().as_bool(), Some(true));
    assert_eq!(a.get("contiguous_prefix").unwrap().as_i64(), Some(0));
    assert_eq!(a.get("expected_total_bytes").unwrap().as_i64(), Some(32));

    // 首片到达 -> 完成
    let resp = handle_request(&mut st, &frag_req(4, 0, true, &[1u8; 8]), 0);
    assert_eq!(event_of(&resp), "completed");
    let mut expected = vec![1u8; 8];
    expected.extend_from_slice(&[2u8; 16]);
    expected.extend_from_slice(&[3u8; 8]);
    assert_eq!(
        resp.get("sha256").unwrap().as_str(),
        Some(sha256_hex(&expected).as_str())
    );
}

#[test]
fn scenario_id_reuse_resets_old_assembly() {
    let mut st = ServerState::new(test_config());
    // 旧数据报 id=5：首片 + 一个中间片（缺末片）
    ok(&handle_request(
        &mut st,
        &frag_req(5, 0, true, &[0xaau8; 8]),
        0,
    ));
    ok(&handle_request(
        &mut st,
        &frag_req(5, 8, true, &[0xbbu8; 8]),
        10,
    ));

    // 新的（不同内容）首片到达 -> id_reuse_reset
    let resp = handle_request(&mut st, &frag_req(5, 0, true, &[0xccu8; 16]), 20);
    ok(&resp);
    assert_eq!(event_of(&resp), "id_reuse_reset");
    assert_eq!(resp.get("discarded_bytes").unwrap().as_i64(), Some(16));

    // 新数据报完成
    let resp = handle_request(&mut st, &frag_req(5, 16, false, &[0xddu8; 8]), 20);
    assert_eq!(event_of(&resp), "completed");
    let mut expected = vec![0xccu8; 16];
    expected.extend_from_slice(&[0xdd; 8]);
    assert_eq!(
        resp.get("sha256").unwrap().as_str(),
        Some(sha256_hex(&expected).as_str())
    );

    let stats = handle_request(&mut st, &obj(vec![("op", s("stats"))]), 20);
    assert_eq!(
        stats
            .get("stats")
            .unwrap()
            .get("id_reuse_resets")
            .unwrap()
            .as_i64(),
        Some(1)
    );
}

#[test]
fn scenario_timeout_purge_reclaims_buffers() {
    let mut st = ServerState::new(test_config());
    ok(&handle_request(
        &mut st,
        &frag_req_at(6, 0, true, &[1u8; 8], 0),
        0,
    ));
    ok(&handle_request(
        &mut st,
        &frag_req_at(7, 0, true, &[2u8; 16], 0),
        0,
    ));
    assert_eq!(
        handle_request(&mut st, &obj(vec![("op", s("status"))]), 0)
            .get("count")
            .unwrap()
            .as_i64(),
        Some(2)
    );

    // ttl=500ms：now=500 时两者都过期
    let resp = handle_request(
        &mut st,
        &obj(vec![("op", s("purge")), ("now_ms", i(500))]),
        500,
    );
    ok(&resp);
    assert_eq!(resp.get("removed_assemblies").unwrap().as_i64(), Some(2));
    assert_eq!(resp.get("reclaimed_bytes").unwrap().as_i64(), Some(24));

    let stats = handle_request(&mut st, &obj(vec![("op", s("stats"))]), 500);
    assert_eq!(
        stats
            .get("stats")
            .unwrap()
            .get("assemblies_expired")
            .unwrap()
            .as_i64(),
        Some(2)
    );
    assert_eq!(
        stats
            .get("stats")
            .unwrap()
            .get("bytes_used")
            .unwrap()
            .as_i64(),
        Some(0)
    );
}

#[test]
fn scenario_packet_hex_goes_through_handwritten_ipv4_parser() {
    use ipfrag::ipv4::{build_ipv4, HeaderParams};
    use std::net::Ipv4Addr;

    let mut st = ServerState::new(test_config());
    let payload: Vec<u8> = (0..60u32).map(|i| i as u8).collect();
    let pieces = split(&payload, 24);

    let mut completed = None;
    // 乱序 + 原始 IPv4 线格式
    for idx in (0..pieces.len()).rev() {
        let (off, data, mf) = &pieces[idx];
        let raw = build_ipv4(&HeaderParams {
            src: Ipv4Addr::new(172, 16, 0, 1),
            dst: Ipv4Addr::new(172, 16, 0, 2),
            protocol: 6,
            identification: 0x999,
            more_fragments: *mf,
            fragment_offset: *off,
            payload: data.clone(),
        });
        let req = obj(vec![
            ("op", s("frag")),
            ("packet_hex", s(hex::encode(&raw))),
            ("include_payload", b(true)),
        ]);
        let resp = handle_request(&mut st, &req, 0);
        ok(&resp);
        if event_of(&resp) == "completed" {
            completed = Some(resp);
        }
    }
    let resp = completed.expect("packet_hex datagram should complete");
    assert_eq!(resp.get("total_bytes").unwrap().as_i64(), Some(60));
    assert_eq!(
        resp.get("sha256").unwrap().as_str(),
        Some(sha256_hex(&payload).as_str())
    );
    // include_payload 回显的字节必须与原始载荷逐字节一致
    let returned =
        ipfrag::server::decode_hex(resp.get("payload_hex").unwrap().as_str().unwrap()).unwrap();
    assert_eq!(returned, payload);
}

#[test]
fn scenario_bad_packet_and_bad_hex_are_reported_with_clear_errors() {
    let mut st = ServerState::new(test_config());

    // 奇数长度 hex
    let resp = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("frag")),
            ("src", s("10.1.0.1")),
            ("dst", s("10.1.0.2")),
            ("protocol", i(17)),
            ("id", i(8)),
            ("offset", i(0)),
            ("mf", b(true)),
            ("payload_hex", s("abc")),
        ]),
        0,
    );
    assert_eq!(resp.get("error").unwrap().as_str(), Some("bad_hex"));

    // 损坏的 IPv4 报文（version=6）
    let resp = handle_request(
        &mut st,
        &obj(vec![("op", s("frag")), ("packet_hex", s("6500"))]),
        0,
    );
    // 2 字节连最小头部都不够 -> Incomplete
    assert_eq!(resp.get("error").unwrap().as_str(), Some("bad_packet"));

    // 未知操作
    let resp = handle_request(&mut st, &obj(vec![("op", s("nope"))]), 0);
    assert_eq!(resp.get("error").unwrap().as_str(), Some("unknown_op"));

    // 非法 JSON
    let parsed = json::parse("{not json");
    assert!(parsed.is_err());
}

#[test]
fn scenario_non_final_fragment_payload_must_be_8_aligned() {
    let mut st = ServerState::new(test_config());

    // 显式字段：MF=true 但负载 5 字节（非 8 倍数）-> bad_request
    let resp = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("frag")),
            ("src", s("10.1.0.1")),
            ("dst", s("10.1.0.2")),
            ("protocol", i(17)),
            ("id", i(30)),
            ("offset", i(0)),
            ("mf", b(true)),
            ("payload_hex", s("0102030405")),
        ]),
        0,
    );
    assert_eq!(resp.get("ok").and_then(|v| v.as_bool()), Some(false));
    assert_eq!(
        resp.get("error").and_then(|v| v.as_str()),
        Some("bad_request")
    );
    assert!(
        resp.get("message")
            .and_then(|v| v.as_str())
            .unwrap()
            .contains("multiple of 8"),
        "got: {}",
        json::to_string(&resp)
    );

    // MF=false（末片）负载任意长度都合法，例如整体只有 5 字节的未分片透传
    let resp = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("frag")),
            ("src", s("10.1.0.1")),
            ("dst", s("10.1.0.2")),
            ("protocol", i(17)),
            ("id", i(31)),
            ("offset", i(0)),
            ("mf", b(false)),
            ("payload_hex", s("0102030405")),
        ]),
        0,
    );
    ok(&resp);
    assert_eq!(event_of(&resp), "completed");

    // packet_hex：构造 MF=1、负载 10 字节的“线格式”报文 -> bad_packet
    use ipfrag::ipv4::build_ipv4;
    use ipfrag::HeaderParams;
    use std::net::Ipv4Addr;
    // build_ipv4 对非末片的非对齐偏移有断言，但不限制负载长度；10 字节负载可构造，
    // 由服务端语义层拒绝（MF=1 负载须为 8 的倍数）。
    let raw = build_ipv4(&HeaderParams {
        src: Ipv4Addr::new(10, 1, 0, 1),
        dst: Ipv4Addr::new(10, 1, 0, 2),
        protocol: 17,
        identification: 32,
        more_fragments: true,
        fragment_offset: 0,
        payload: vec![0xab; 10],
    });
    let resp = handle_request(
        &mut st,
        &obj(vec![
            ("op", s("frag")),
            ("packet_hex", s(hex::encode(&raw))),
        ]),
        0,
    );
    assert_eq!(
        resp.get("error").and_then(|v| v.as_str()),
        Some("bad_packet")
    );
}

#[test]
fn scenario_memory_budget_and_datagram_limits_surface_named_errors() {
    let cfg = ReassemblerConfig {
        mem_budget_bytes: 32,
        max_datagram_bytes: 48,
        purge_interval_ms: u64::MAX,
        ..test_config()
    };
    let mut st = ServerState::new(cfg);

    ok(&handle_request(
        &mut st,
        &frag_req(20, 0, true, &[0u8; 24]),
        0,
    ));
    let resp = handle_request(&mut st, &frag_req(21, 0, true, &[0u8; 24]), 0);
    assert_eq!(
        resp.get("error").unwrap().as_str(),
        Some("memory_budget_exceeded")
    );

    // 末片声明总长度 56 > 48
    let resp = handle_request(&mut st, &frag_req(20, 48, false, &[0u8; 8]), 0);
    assert_eq!(
        resp.get("error").unwrap().as_str(),
        Some("datagram_too_large")
    );
}
