//! 离线 IPv4 分片重组演示：乱序 + 重复 + 缺首片 + 重叠拒绝。
//!
//! 运行：`cargo run --example reassembly_demo`

use ipfrag::ipv4;
use ipfrag::reassembly::{AddResult, Config, Reassembler};

const S: (u8, u8, u8, u8) = (192, 168, 0, 10);
const D: (u8, u8, u8, u8) = (203, 0, 113, 7);

fn main() {
    let mut engine = Reassembler::new(Config {
        fragment_ttl_ms: 30_000,
        total_memory_budget: 1 << 20,
        max_datagram_payload: 65_535,
    });

    // 原始载荷 3001 字节（尾片非 8 倍数，验证偏移换算）
    let original: Vec<u8> = (0..3001u32).map(|x| (x % 251) as u8).collect();
    let cuts = [0usize, 1480, 2960, 3001];
    let id = 0xBEEF;
    let frag = |i: usize| -> Vec<u8> {
        // 除最后一片外都置 MF
        let mf = i < cuts.len() - 2;
        ipv4::build_fragment(
            S,
            D,
            17,
            id,
            cuts[i],
            mf,
            original[cuts[i]..cuts[i + 1]].to_vec(),
        )
    };

    let order = [1usize, 1, 2, 0]; // 片2、片2(重复)、尾片、首片
    for (step, &i) in order.iter().enumerate() {
        match engine.add_packet(&frag(i), step as u64).unwrap() {
            AddResult::Pending(s) => println!(
                "step {step}: fragment {} accepted -> pending(fragments={}, contiguous={}, total={:?})",
                i + 1,
                s.fragment_count,
                s.contiguous_from_zero,
                s.total_length
            ),
            AddResult::Completed(d) => {
                assert_eq!(d.payload, original, "payload must match original exactly");
                println!("step {step}: fragment {} COMPLETED datagram: {} bytes (matches original: {})",
                    i + 1, d.payload.len(), d.payload == original);
            }
        }
    }

    // 重叠片演示（另一个 ID）
    engine
        .add_packet(
            &ipv4::build_fragment(S, D, 17, 0xDEAD, 0, true, vec![0x11; 16]),
            100,
        )
        .unwrap();
    let err = engine
        .add_packet(
            &ipv4::build_fragment(S, D, 17, 0xDEAD, 8, true, vec![0x22; 16]),
            101,
        )
        .unwrap_err();
    println!("overlap correctly rejected: {err}");
    assert_eq!(engine.pending_count(), 0);

    println!("OK: offline reassembly demo finished");
}
