//! # 验收测试：IPv4 分片重组
//!
//! 覆盖任务要求的六个场景，并在每个完成场景中把重组结果与
//! **完整原始载荷**逐字节对照：
//!
//! 1. 乱序到达
//! 2. 重复片（完全重复，幂等）
//! 3. 重叠片（拒绝，整体丢弃数据报）
//! 4. 缺首片（不完成，补齐后完成）
//! 5. ID 重用（旧组被新数据报替换）
//! 6. 超时清理（显式 + 惰性）
//!
//! 另含：内存预算、单包长度上限、未分片报文、原始 IPv4 路径。

use std::net::Ipv4Addr;

use ipfrag::ipv4;
use ipfrag::reassembly::{AddResult, Config, Key, Reassembler, ReassemblyError};

type Quad = (u8, u8, u8, u8);
const S: Quad = (172, 16, 0, 1);
const D: Quad = (172, 16, 0, 2);

fn frag(id: u16, off: usize, mf: bool, data: &[u8]) -> Vec<u8> {
    ipv4::build_fragment(S, D, 17, id, off, mf, data.to_vec())
}

fn key(id: u16) -> Key {
    Key {
        src: Ipv4Addr::new(S.0, S.1, S.2, S.3),
        dst: Ipv4Addr::new(D.0, D.1, D.2, D.3),
        protocol: 17,
        identification: id,
    }
}

fn engine(ttl_ms: u64) -> Reassembler {
    Reassembler::new(Config {
        fragment_ttl_ms: ttl_ms,
        total_memory_budget: 1 << 20,
        max_datagram_payload: 65_535,
    })
}

/// 把任意长度载荷切成 8 字节对齐的分片（尾片除外）。
fn split_payload(payload: &[u8], chunk: usize) -> Vec<(usize, bool, Vec<u8>)> {
    assert!(chunk.is_multiple_of(8));
    let mut pieces = Vec::new();
    let mut off = 0;
    while off < payload.len() {
        let take = chunk.min(payload.len() - off);
        let mf = off + take < payload.len();
        pieces.push((off, mf, payload[off..off + take].to_vec()));
        off += take;
    }
    pieces
}

#[test]
fn acceptance_1_out_of_order_matches_original() {
    let mut e = engine(30_000);
    let payload: Vec<u8> = (0..10_000u32)
        .map(|x| (x.wrapping_mul(7) % 256) as u8)
        .collect();

    let mut pieces = split_payload(&payload, 1480);
    // 确定性乱序：尾片先到，然后交错
    pieces.reverse();
    pieces.rotate_left(2);

    let mut completed = None;
    for (i, (off, mf, data)) in pieces.iter().enumerate() {
        let pkt = frag(0x1111, *off, *mf, data);
        if let AddResult::Completed(d) = e.add_packet(&pkt, i as u64).unwrap() {
            completed = Some(d);
        }
    }
    let d = completed.expect("datagram must complete from out-of-order fragments");
    assert_eq!(d.payload.len(), payload.len());
    assert_eq!(
        d.payload, payload,
        "reassembly must equal the full original payload"
    );
    assert_eq!(e.buffered_bytes(), 0);
}

#[test]
fn acceptance_2_duplicates_are_idempotent() {
    let mut e = engine(30_000);
    let p1 = frag(0x2222, 0, true, &[1u8; 80]);
    let p2 = frag(0x2222, 80, false, &[2u8; 16]);

    // 首片重复送 3 次（不同时间，刷新寿命）
    for now in [0u64, 10, 20] {
        let s = match e.add_packet(&p1, now).unwrap() {
            AddResult::Pending(s) => s,
            AddResult::Completed(d) => panic!("unexpected completion: {}", d.payload.len()),
        };
        assert_eq!(s.fragment_count, 1);
        assert_eq!(s.buffered_bytes, 80);
    }
    assert_eq!(e.buffered_bytes(), 80);

    let d = e.add_packet(&p2, 30).unwrap().into_completed().unwrap();
    let mut expected = vec![1u8; 80];
    expected.extend(std::iter::repeat_n(2u8, 16));
    assert_eq!(d.payload, expected);
}

#[test]
fn acceptance_3_overlap_is_rejected_and_drops_group() {
    let mut e = engine(30_000);
    e.add_packet(&frag(0x3333, 0, true, &[0xA0; 16]), 0)
        .unwrap();
    // 合法偏移 8，非尾片 16 字节，与首片在 [8,16) 交叠
    let err = e
        .add_packet(&frag(0x3333, 8, true, &[0xB0; 16]), 1)
        .unwrap_err();
    assert!(
        matches!(err, ReassemblyError::OverlapConflict { .. }),
        "expected overlap rejection, got: {err}"
    );
    assert_eq!(e.pending_count(), 0, "whole datagram must be discarded");
    assert_eq!(e.buffered_bytes(), 0);
    assert!(e.status(&key(0x3333)).is_none());

    // 同样几何但交叠字节相同：仍然拒绝（明确的保守策略）
    e.add_packet(&frag(0x4444, 0, true, &[0xC0; 16]), 0)
        .unwrap();
    let err = e
        .add_packet(
            &frag(0x4444, 8, true, &{
                let mut v = vec![0xC0; 8];
                v.extend(std::iter::repeat_n(0xC1, 8));
                v
            }),
            1,
        )
        .unwrap_err();
    assert!(matches!(err, ReassemblyError::OverlapConflict { .. }));
}

#[test]
fn acceptance_4_missing_first_fragment_blocks_completion() {
    let mut e = engine(30_000);
    // 只送中片与尾片
    let mid = frag(0x5555, 1480, true, &[0xDD; 1480]);
    let tail = frag(0x5555, 2960, false, &[0xEE; 13]);
    let s = match e.add_packet(&mid, 0).unwrap() {
        AddResult::Pending(s) => s,
        _ => panic!(),
    };
    assert_eq!(s.contiguous_from_zero, 0);
    let s = match e.add_packet(&tail, 1).unwrap() {
        AddResult::Pending(s) => s,
        _ => panic!(),
    };
    assert_eq!(s.total_length, Some(2973));
    assert_eq!(s.contiguous_from_zero, 0, "hole at the beginning remains");

    // 首片迟到后完成，对照原始载荷
    let mut original = vec![0xCC; 1480];
    original.extend(std::iter::repeat_n(0xDD, 1480));
    original.extend(std::iter::repeat_n(0xEE, 13));
    let head = frag(0x5555, 0, true, &original[..1480]);
    let d = e.add_packet(&head, 2).unwrap().into_completed().unwrap();
    assert_eq!(d.payload, original);
}

#[test]
fn acceptance_5_id_reuse_resets_incomplete_group() {
    let mut e = engine(30_000);
    // 旧数据报（有洞）
    e.add_packet(&frag(0x6666, 0, true, &[1u8; 16]), 0).unwrap();
    e.add_packet(&frag(0x6666, 32, false, &[2u8; 8]), 1)
        .unwrap();
    assert_eq!(e.pending_count(), 1);

    // 新数据报复用同一四元组+ID：首片内容不同 → 判定重用
    let new_head = frag(0x6666, 0, true, &[9u8; 24]);
    let s = match e.add_packet(&new_head, 2).unwrap() {
        AddResult::Pending(s) => s,
        _ => panic!(),
    };
    assert_eq!(s.fragment_count, 1, "old group must have been reset");
    assert_eq!(s.buffered_bytes, 24);

    let new_tail = frag(0x6666, 24, false, &[8u8; 8]);
    let d = e
        .add_packet(&new_tail, 3)
        .unwrap()
        .into_completed()
        .unwrap();
    let mut expected = vec![9u8; 24];
    expected.extend(std::iter::repeat_n(8u8, 8));
    assert_eq!(d.payload, expected);
}

#[test]
fn acceptance_6_timeout_cleanup_explicit_and_lazy() {
    let mut e = engine(500);
    e.add_packet(&frag(0x7777, 0, true, &[0; 8]), 1_000)
        .unwrap();
    e.add_packet(&frag(0x7778, 0, true, &[0; 8]), 1_100)
        .unwrap();

    // 显式清理：7777 在 1500 到期，7778 未到期
    let purged = e.purge_expired(1_500);
    assert_eq!(purged.len(), 1);
    assert_eq!(purged[0].0.identification, 0x7777);
    assert_eq!(e.pending_count(), 1);
    assert_eq!(e.buffered_bytes(), 8);

    // 惰性清理：下一次送片时，过期组（1100+500=1600）在 2000 被清掉
    e.add_packet(&frag(0x7779, 0, true, &[1; 8]), 2_000)
        .unwrap();
    assert_eq!(
        e.pending_count(),
        1,
        "7778 expired lazily; only 7779 remains"
    );
    assert_eq!(e.buffered_bytes(), 8);
}

#[test]
fn budget_and_oversize_limits_are_reported_distinctly() {
    // 总内存预算
    let mut e = Reassembler::new(Config {
        fragment_ttl_ms: 10_000,
        total_memory_budget: 64,
        max_datagram_payload: 65_535,
    });
    e.add_packet(&frag(0x8888, 0, true, &[0; 64]), 0).unwrap();
    let err = e
        .add_packet(&frag(0x8889, 0, true, &[0; 8]), 1)
        .unwrap_err();
    assert!(
        matches!(err, ReassemblyError::BudgetExceeded { .. }),
        "{err}"
    );
    assert_eq!(e.buffered_bytes(), 64);

    // 单数据报长度上限
    let mut e2 = Reassembler::new(Config {
        fragment_ttl_ms: 10_000,
        total_memory_budget: 1 << 20,
        max_datagram_payload: 32,
    });
    let err = e2
        .add_packet(&frag(0x9999, 0, true, &[0; 40]), 0)
        .unwrap_err();
    assert!(matches!(err, ReassemblyError::OversizedDatagram { .. }));
}

#[test]
fn unfragmented_datagram_completes_immediately_via_raw_path() {
    let mut e = engine(30_000);
    let packet = ipv4::build_fragment(S, D, 1, 0xAAAA, 0, false, (0..=255).collect());
    let d = e.add_packet(&packet, 0).unwrap().into_completed().unwrap();
    let expected: Vec<u8> = (0..=255).collect();
    assert_eq!(d.payload, expected);
    assert_eq!(d.fragment_count, 1);
}

#[test]
fn malformed_raw_packet_is_classified_error() {
    let mut e = engine(30_000);
    let err = e.add_packet(&[0x45, 0x00], 0).unwrap_err();
    assert!(matches!(
        err,
        ReassemblyError::ParseFailed(ipfrag::ipv4::Ipv4Error::Truncated { .. })
    ));
}
