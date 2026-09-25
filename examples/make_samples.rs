//! 生成 NDJSON 请求样例（输出到 stdout，重定向到文件即可）。
//!
//! 生成的样例是一个完整故事线：乱序送 3 个分片（含 1 个完全重复片），
//! 引擎在最后一片到达时重组出与原始载荷一致的数据报。
//!
//! 运行：`cargo run --example make_samples > examples/requests.ndjson`

use ipfrag::ipv4;
use ipfrag::server::hex_encode;

fn main() {
    let src = (192, 168, 0, 1);
    let dst = (10, 0, 0, 200);
    let protocol = 17u8;
    let id = 4660u16; // 0x1234

    // 原始载荷 300 字节，切成 136 + 136 + 28（前两片 8 字节对齐）
    let payload: Vec<u8> = (0..300u32)
        .map(|x| (x.wrapping_mul(37) % 256) as u8)
        .collect();

    let p1 = ipv4::build_fragment(src, dst, protocol, id, 0, true, payload[0..136].to_vec());
    let p2 = ipv4::build_fragment(
        src,
        dst,
        protocol,
        id,
        136,
        true,
        payload[136..272].to_vec(),
    );
    let p3 = ipv4::build_fragment(src, dst, protocol, id, 272, false, payload[272..].to_vec());

    // raw_packet 样例：一个未分片的小 UDP 报文（offset=0, MF=0）
    let raw = ipv4::build_fragment(
        src,
        dst,
        protocol,
        777,
        0,
        false,
        vec![0xDE, 0xAD, 0xBE, 0xEF],
    );

    // 输出 NDJSON。故意乱序：片2 → 片2重复 → 片3 → 片1。
    println!(
        r#"{{"op":"raw_packet","packet_hex":"{}","now_ms":0}}"#,
        hex_encode(&raw)
    );
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":136,"mf":true,"payload_hex":"{}","now_ms":10}}"#,
        hex_encode(&payload[136..272])
    );
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":136,"mf":true,"payload_hex":"{}","now_ms":11}}"#,
        hex_encode(&payload[136..272])
    );
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":272,"mf":false,"payload_hex":"{}","now_ms":20}}"#,
        hex_encode(&payload[272..])
    );
    println!(r#"{{"op":"status"}}"#);
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4660,"offset":0,"mf":true,"payload_hex":"{}","now_ms":30}}"#,
        hex_encode(&payload[0..136])
    );

    // 重叠冲突样例（另一组 id=4661）
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4661,"offset":0,"mf":true,"payload_hex":"{}","now_ms":40}}"#,
        hex_encode(&[0x11u8; 16])
    );
    println!(
        r#"{{"op":"fragment","src":"192.168.0.1","dst":"10.0.0.200","protocol":17,"id":4661,"offset":8,"mf":true,"payload_hex":"{}","now_ms":41}}"#,
        hex_encode(&[0x22u8; 16])
    );

    // 超时清理样例（默认 TTL 30000ms）
    println!(r#"{{"op":"purge","now_ms":40000}}"#);
    println!(r#"{{"op":"reset"}}"#);

    // 同时把原始载荷写到 stderr，供人工对照（重组响应里的 payload_hex 应等于它）
    eprintln!(
        "# original_payload_hex(300 bytes) = {}",
        hex_encode(&payload)
    );
    let _ = (p1, p2, p3);
}
