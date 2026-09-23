//! 协议操作测试：长度前缀分帧 + CRC-32 必须与独立参考实现一致，
//! 截断帧必须拒绝且不提交。

mod common;

use common::{receipt_output, Harness};
use gas_meter::{ExecStatus, MeteringVersion};

/// 独立的 CRC-32/ISO-HDLC（多项式 0xEDB88320 反射）参考实现，
/// 与来宾代码无共享。
fn crc32_ieee(data: &[u8]) -> u32 {
    let mut table = [0u32; 256];
    for i in 0..256u32 {
        let mut c = i;
        for _ in 0..8 {
            c = if c & 1 == 1 {
                (c >> 1) ^ 0xEDB88320
            } else {
                c >> 1
            };
        }
        table[i as usize] = c;
    }
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc = (crc >> 8) ^ table[((crc ^ b as u32) & 0xFF) as usize];
    }
    crc ^ 0xFFFF_FFFF
}

fn encode_raw_frames(frames: &[&[u8]]) -> Vec<u8> {
    let mut blob = Vec::new();
    for f in frames {
        blob.extend_from_slice(&(f.len() as u16).to_le_bytes());
        blob.extend_from_slice(f);
    }
    let mut input = (blob.len() as u32).to_le_bytes().to_vec();
    input.extend(blob);
    input
}

#[test]
fn framed_crc32_roundtrip_matches_reference() {
    let h = Harness::new();
    let frames: &[&[u8]] = &[
        b"hello",
        b"",
        b"protocol-frame-1234567890",
        &(0..=255u8).collect::<Vec<u8>>(),
    ];
    let input = encode_raw_frames(frames);
    let r = h.sample("frame_protocol", MeteringVersion::V1, &input);
    assert_eq!(r.status, ExecStatus::Success, "{}", r.termination_reason);

    let out = receipt_output(&r);
    let mut pos = 0;
    let mut got_frames = 0;
    while pos < out.len() {
        let len = u16::from_le_bytes(out[pos..pos + 2].try_into().unwrap()) as usize;
        pos += 2;
        let payload = &out[pos..pos + len];
        pos += len;
        let crc = u32::from_le_bytes(out[pos..pos + 4].try_into().unwrap());
        pos += 4;
        assert_eq!(payload, frames[got_frames], "frame {got_frames} payload");
        assert_eq!(crc, crc32_ieee(payload), "frame {got_frames} crc32");
        got_frames += 1;
    }
    assert_eq!(got_frames, frames.len());

    // KV 提交帧数
    let committed = h.host.current();
    assert_eq!(
        committed.get("proto").map(|v| v.as_slice()),
        Some(4u32.to_le_bytes().as_slice())
    );
}

#[test]
fn truncated_frame_is_rejected_without_commit() {
    let h = Harness::new();
    // 帧区声明 3 字节，内含 len=10 但只有 2 字节载荷
    let mut input = 3u32.to_le_bytes().to_vec();
    input.extend_from_slice(&10u16.to_le_bytes());
    input.extend_from_slice(b"ab");
    let r = h.sample("frame_protocol", MeteringVersion::V1, &input);
    assert_eq!(r.status, ExecStatus::ModuleAbort);
    assert!(!r.committed);
    assert!(h.host.is_empty());
}

#[test]
fn crc32_table_sanity_known_values() {
    // 参考实现自检
    assert_eq!(crc32_ieee(b""), 0);
    assert_eq!(crc32_ieee(b"123456789"), 0xCBF43926);
}
