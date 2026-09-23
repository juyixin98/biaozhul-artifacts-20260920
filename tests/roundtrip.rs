//! 编码器 ↔ 解码器往返测试：
//! `OutEvent` 编码出的字节必须能被增量解码器还原成等价事件。

mod common;

use sse_resume::decode::Decoder;
use sse_resume::encode::{encode_to_vec, OutEvent};
use sse_resume::error::EncodeError;
use common::Lcg;

fn decode_all(bytes: &[u8]) -> Vec<sse_resume::Event> {
    let mut d = Decoder::new();
    let mut v = d.push(bytes).unwrap();
    v.append(&mut d.finish().unwrap());
    v
}

fn decode_random(bytes: &[u8], seed: u64, max_chunk: usize) -> Vec<sse_resume::Event> {
    let mut rng = Lcg(seed);
    let mut d = Decoder::new();
    let mut pos = 0;
    let mut out = Vec::new();
    while pos < bytes.len() {
        let len = rng.range(1, max_chunk).min(bytes.len() - pos);
        out.append(&mut d.push(&bytes[pos..pos + len]).unwrap());
        pos += len;
    }
    out.append(&mut d.finish().unwrap());
    out
}

#[test]
fn full_event_roundtrips() {
    let ev = OutEvent {
        id: Some("e-1".into()),
        event: Some("order.created".into()),
        retry_ms: Some(2500),
        data: vec!["first".into(), "second".into()],
    };
    let bytes = encode_to_vec(&ev).unwrap();
    let parsed = decode_all(&bytes);
    assert_eq!(parsed.len(), 1);
    assert_eq!(parsed[0].event, "order.created");
    assert_eq!(parsed[0].id.as_deref(), Some("e-1"));
    assert_eq!(parsed[0].data, "first\nsecond");

    // 随机分块下也一致。
    for seed in 0..16u64 {
        let p = decode_random(&bytes, seed, 5);
        assert_eq!(p.len(), 1, "seed {seed}");
        assert_eq!(p[0].data, "first\nsecond");
        assert_eq!(p[0].id.as_deref(), Some("e-1"));
    }
}

#[test]
fn default_message_type_roundtrips() {
    let ev = OutEvent::data_line("hello");
    let bytes = encode_to_vec(&ev).unwrap();
    let parsed = decode_all(&bytes);
    assert_eq!(parsed[0].event, "message");
    assert_eq!(parsed[0].data, "hello");
    assert_eq!(parsed[0].id, None);
}

#[test]
fn empty_id_encodes_then_clears_cursor() {
    // 先发一个带 id 的事件建立游标，再发空 id 事件。
    let mut stream = Vec::new();
    stream.extend(encode_to_vec(&OutEvent::data_line("a").with_id("55")).unwrap());
    stream.extend(encode_to_vec(&OutEvent::data_line("b").with_id("")).unwrap());

    let parsed = decode_all(&stream);
    assert_eq!(parsed.len(), 2);
    assert_eq!(parsed[0].id.as_deref(), Some("55"));
    // 裸 `id\n` 使游标变为空串。
    assert_eq!(parsed[1].id.as_deref(), Some(""));
}

#[test]
fn empty_data_is_an_emittable_empty_event() {
    let ev = OutEvent {
        id: None,
        event: None,
        retry_ms: None,
        data: vec![],
    };
    let bytes = encode_to_vec(&ev).unwrap();
    let parsed = decode_all(&bytes);
    assert_eq!(parsed.len(), 1);
    assert_eq!(parsed[0].data, "");
}

#[test]
fn consecutive_events_preserve_order_and_fields() {
    let events = vec![
        OutEvent::data_line("d0").with_id("100"),
        OutEvent {
            id: Some("101".into()),
            event: Some("x".into()),
            retry_ms: Some(10),
            data: vec!["l1".into(), "l2".into(), "l3".into()],
        },
        OutEvent::data_line("d2"), // 无 id：解码后 id 沿用 last id
    ];
    let mut bytes = Vec::new();
    for e in &events {
        bytes.extend(encode_to_vec(e).unwrap());
    }
    let parsed = decode_all(&bytes);
    assert_eq!(parsed.len(), 3);
    assert_eq!(parsed[0].id.as_deref(), Some("100"));
    assert_eq!(parsed[1].event, "x");
    assert_eq!(parsed[1].data, "l1\nl2\nl3");
    assert_eq!(parsed[2].event, "message");
    assert_eq!(parsed[2].data, "d2");
    assert_eq!(parsed[2].id.as_deref(), Some("101")); // 沿用
}

#[test]
fn newline_in_field_value_is_rejected_not_injected() {
    // 帧注入防护：不允许把换行塞进字段值。
    let err = encode_to_vec(&OutEvent::data_line("bad\nid: pwned")).unwrap_err();
    assert!(matches!(
        err,
        EncodeError::ValueContainsNewline { field: "data" }
    ));
    let err = encode_to_vec(&OutEvent::data_line("x").with_id("1\nevent: pwn")).unwrap_err();
    assert!(matches!(
        err,
        EncodeError::ValueContainsNewline { field: "id" }
    ));
}

#[test]
fn comment_lines_decode_to_no_events() {
    let mut bytes = Vec::new();
    sse_resume::encode::encode_comment_into(&mut bytes, "keepalive").unwrap();
    bytes.extend(encode_to_vec(&OutEvent::data_line("real")).unwrap());
    let parsed = decode_all(&bytes);
    assert_eq!(parsed.len(), 1);
    assert_eq!(parsed[0].data, "real");
}

#[test]
fn fuzz_roundtrip_many_singleline_events() {
    // 确定性“模糊”：生成一批含各种可打印字符（不含换行）的数据，往返后必须一致。
    let mut rng = Lcg(0xABCD);
    let mut bytes = Vec::new();
    let mut expected = Vec::new();
    for i in 0..300u32 {
        let len = rng.range(0, 40);
        let mut s = String::new();
        for _ in 0..len {
            // 避免控制字符与换行（那些会改变帧结构），取常见可见 Unicode 范围。
            let c = 32u32 + rng.range(0, 0x7E - 32) as u32;
            if c == 0x7F {
                continue;
            }
            if let Some(ch) = char::from_u32(c) {
                s.push(ch);
            }
        }
        let ev = OutEvent::data_line(s.clone()).with_id(i.to_string());
        bytes.extend(encode_to_vec(&ev).unwrap());
        expected.push((i.to_string(), s));
    }

    for seed in 0..12u64 {
        let parsed = decode_random(&bytes, seed.wrapping_add(12345), 11);
        assert_eq!(parsed.len(), expected.len(), "seed {seed} 事件数不符");
        for (ev, (id, data)) in parsed.iter().zip(&expected) {
            assert_eq!(ev.id.as_deref(), Some(id.as_str()));
            assert_eq!(&ev.data, data);
        }
    }
}
