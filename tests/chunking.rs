//! 增量解析的“跨块切分”测试：同一批 SSE 字节，在**所有**切分位置 /
//! 随机分块 / 逐字节喂入时，解码结果必须一致。
//!
//! 覆盖验收点：CR、LF、CRLF 跨行终止符，以及它们落在 TCP 块边界上的情况。

mod common;

use common::Lcg;
use sse_resume::decode::{Decoder, Event};

/// 喂入完整字节，期望得到的事件序列（基准）。
fn decode_whole(bytes: &[u8]) -> Vec<Event> {
    let mut d = Decoder::new();
    let mut v = d.push(bytes).unwrap();
    v.append(&mut d.finish().unwrap());
    v
}

/// 在每一个可能的切分点切成两块（i=0..=len，含空前块/空后块）。
fn decode_two_chunks(bytes: &[u8]) -> Vec<Vec<Event>> {
    let mut out = Vec::with_capacity(bytes.len() + 1);
    for i in 0..=bytes.len() {
        let mut d = Decoder::new();
        let mut v = d.push(&bytes[..i]).unwrap();
        v.append(&mut d.push(&bytes[i..]).unwrap());
        v.append(&mut d.finish().unwrap());
        out.push(v);
    }
    out
}

/// 按给定确定性随机块长序列喂入。
fn decode_random_chunks(bytes: &[u8], seed: u64, max_chunk: usize) -> Vec<Event> {
    let mut rng = Lcg(seed);
    let mut d = Decoder::new();
    let mut pos = 0usize;
    let mut events = Vec::new();
    while pos < bytes.len() {
        let len = rng.range(1, max_chunk).min(bytes.len() - pos);
        events.append(&mut d.push(&bytes[pos..pos + len]).unwrap());
        pos += len;
    }
    events.append(&mut d.finish().unwrap());
    events
}

fn decode_byte_by_byte(bytes: &[u8]) -> Vec<Event> {
    let mut d = Decoder::new();
    let mut events = Vec::new();
    for &b in bytes {
        events.append(&mut d.push(&[b]).unwrap());
    }
    events.append(&mut d.finish().unwrap());
    events
}

/// 对一帧字节做“所有切分方式都应得到同一结果”的完整断言。
fn assert_split_invariant(label: &str, bytes: &[u8], expected_count: usize) {
    let whole = decode_whole(bytes);
    assert_eq!(
        whole.len(),
        expected_count,
        "[{label}] 基准解码事件数不符: {whole:?}"
    );

    let two = decode_two_chunks(bytes);
    assert_eq!(two.len(), bytes.len() + 1);
    for (i, got) in two.iter().enumerate() {
        assert_eq!(got, &whole, "[{label}] 在切分点 {i} 切成两块时结果不一致");
    }

    let bbb = decode_byte_by_byte(bytes);
    assert_eq!(&bbb, &whole, "[{label}] 逐字节喂入结果不一致");

    for seed in 0..32u64 {
        let got = decode_random_chunks(bytes, seed.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(1), 7);
        assert_eq!(&got, &whole, "[{label}] 随机分块(seed={seed})结果不一致");
    }
    // 也测一次故意大块的。
    let got = decode_random_chunks(bytes, 999, 4096);
    assert_eq!(&got, &whole, "[{label}] 随机大块结果不一致");
}

#[test]
fn lf_splits_everywhere() {
    assert_split_invariant("LF", b"data: a\n\ndata: b\n\n", 2);
}

#[test]
fn crlf_splits_everywhere() {
    // 重点：CRLF 被切开时不能多派发空行。
    assert_split_invariant("CRLF", b"id: 1\r\ndata: a\r\n\r\ndata: b\r\n\r\n", 2);
}

#[test]
fn cr_splits_everywhere() {
    assert_split_invariant("CR", b"id: 1\rdata: a\r\rdata: b\r\r", 2);
}

#[test]
fn mixed_terminators_split() {
    // 同一流中混用 CR / LF / CRLF。
    let bytes = b"data: a\n\rdata: b\r\ndata: c\n\n";
    // 依次产生：a；（CR 终止空行）派发含 "b\ndata: c"?
    // 明确展开：
    //  "data: a\n"  -> data=a\n
    //  "\r"          -> 空行 -> 派发 a
    //  "data: b\r\n"-> data=b\n
    //  "data: c\n"  -> data=b\nc\n
    //  "\n"          -> 派发 "b\nc"
    assert_split_invariant("mixed", bytes, 2);
    let whole = decode_whole(bytes);
    assert_eq!(whole[0].data, "a");
    assert_eq!(whole[1].data, "b\nc");
}

#[test]
fn crlf_boundary_right_at_chunk_end_all_offsets() {
    // 手工构造关键边界：\r 在块尾，\n 在块首；以及 \r\n 在块尾、派发空行在下一块。
    let frame = b"data: x\r\n\r\n";
    let cases: &[&[&[u8]]] = &[
        // (块序列)
        &[b"data: x\r", b"\n\r\n"],
        &[b"data: x\r\n", b"\r\n"],
        &[b"data: x\r\n\r", b"\n"],
        &[b"data: x", b"\r\n\r\n"],
        &[b"", frame], // 空前块
        &[frame, b""], // 空后块
    ];
    let expected = decode_whole(frame);
    assert_eq!(expected.len(), 1);
    for (i, chunks) in cases.iter().enumerate() {
        let mut d = Decoder::new();
        let mut got = Vec::new();
        for c in *chunks {
            got.append(&mut d.push(c).unwrap());
        }
        got.append(&mut d.finish().unwrap());
        assert_eq!(got, expected, "CRLF 边界用例 {i} 失败: {chunks:?}");
    }
}

#[test]
fn multiline_data_and_fields_split() {
    let bytes = b"id: 42\nevent: patch\nretry: 1500\ndata: line1\ndata: line2\n\n";
    assert_split_invariant("fields+multiline", bytes, 1);
    let ev = &decode_whole(bytes)[0];
    assert_eq!(ev.id.as_deref(), Some("42"));
    assert_eq!(ev.event, "patch");
    assert_eq!(ev.data, "line1\nline2");
}

#[test]
fn comments_and_blank_blocks_split() {
    let bytes = b": ping\n: \n\ndata: after\nid: 9\n\n";
    // 开头注释 + 空行（无 data，不派发），然后真正事件。
    assert_split_invariant("comments", bytes, 1);
    assert_eq!(decode_whole(bytes)[0].data, "after");
}

#[test]
fn empty_id_variants_split() {
    let bytes = b"id: 3\ndata: keep\n\nid\ndata: cleared\n\n";
    assert_split_invariant("empty-id", bytes, 2);
    let w = decode_whole(bytes);
    assert_eq!(w[0].id.as_deref(), Some("3"));
    assert_eq!(w[1].id.as_deref(), Some(""));
}

#[test]
fn large_stream_many_random_splits_agree() {
    // 构造一条较长的混合终止符流，做 64 组随机切分，结果必须稳定。
    let mut bytes = Vec::new();
    let mut rng = Lcg(0xC0FFEE);
    for i in 0..200u32 {
        bytes.extend_from_slice(format!("id: {i}").as_bytes());
        bytes.push(match i % 3 {
            0 => b'\n',
            1 => b'\r',
            _ => b'\r',
        });
        if i % 3 == 2 {
            bytes.push(b'\n'); // CRLF
        }
        bytes.extend_from_slice(format!("data: payload-{i}").as_bytes());
        bytes.push(match (i / 3) % 3 {
            0 => b'\n',
            1 => b'\r',
            _ => b'\r',
        });
        if (i / 3) % 3 == 2 {
            bytes.push(b'\n');
        }
        // 派发空行。
        bytes.extend_from_slice(match i % 3 {
            0 => b"\n",
            1 => b"\r",
            _ => b"\r\n",
        });
    }
    let _ = &mut rng;
    let whole = decode_whole(&bytes);
    assert_eq!(whole.len(), 200);
    for seed in 0..64u64 {
        let got = decode_random_chunks(
            &bytes,
            seed.wrapping_mul(1_000_003).wrapping_add(7),
            13,
        );
        assert_eq!(got.len(), 200, "seed {seed} 事件数不符");
        assert_eq!(got, whole, "seed {seed} 内容不符");
    }
}
