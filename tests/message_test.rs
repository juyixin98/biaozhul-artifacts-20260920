//! 报文级测试：A/AAAA/CNAME/未知类型解析、截断资源记录、
//! 已知报文 decode→encode 语义往返（字节级 + 二次解析幂等）。

use dns_compress::message::{Message, Rdata, CLASS_IN, TYPE_A, TYPE_AAAA, TYPE_CNAME};
use dns_compress::DnsError;

fn put16(v: &mut Vec<u8>, x: u16) {
    v.extend_from_slice(&x.to_be_bytes());
}
fn put32(v: &mut Vec<u8>, x: u32) {
    v.extend_from_slice(&x.to_be_bytes());
}

/// 手工构造一个“已知应答报文”（不经过自己的编码器，保证字节是独立来源）：
///
/// ```text
/// 问题： host.example A IN（偏移12，未压缩）
/// 回答：
///   owner 指针->12, CNAME -> target.example（未压缩全名）
///   owner = target.example（指针指向回答1 RDATA 中目标名的起始偏移）
///           A 192.0.2.7
/// 附加： 未知 TYPE=40 RDATA = 原始 6 字节 01 02 03 04 05 06
/// ```
fn known_response() -> Vec<u8> {
    let mut b = Vec::new();
    put16(&mut b, 0xABCD); // id
    put16(&mut b, 0x8180); // QR=1 RD=1 RA=1 RCODE=0
    put16(&mut b, 1); // QD
    put16(&mut b, 2); // AN
    put16(&mut b, 0); // NS
    put16(&mut b, 1); // AR
    assert_eq!(b.len(), 12);

    // 问题名 host.example：1+4 + 1+7 + 1 = 14 字节
    b.extend_from_slice(&[4]);
    b.extend_from_slice(b"host");
    b.extend_from_slice(&[7]);
    b.extend_from_slice(b"example");
    b.push(0);
    // 问题结束于 12 + 14 + 4 = 30
    put16(&mut b, TYPE_A);
    put16(&mut b, CLASS_IN);
    assert_eq!(b.len(), 30);

    // 回答1：owner 指针 -> 12
    put16(&mut b, 0xC00C);
    put16(&mut b, TYPE_CNAME);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 60);
    // RDATA 目标名 target.example（内联，供后面前向/普通指针引用）
    let mut target_name = Vec::new();
    target_name.extend_from_slice(&[6]);
    target_name.extend_from_slice(b"target");
    target_name.extend_from_slice(&[7]);
    target_name.extend_from_slice(b"example");
    target_name.push(0);
    put16(&mut b, target_name.len() as u16);
    let target_name_off = b.len();
    b.extend_from_slice(&target_name);

    // 回答2：owner 指针指向 target_name_off（前向/中间指针均可，这里是后向）
    put16(&mut b, 0xC000 | (target_name_off as u16));
    put16(&mut b, TYPE_A);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 90);
    put16(&mut b, 4);
    b.extend_from_slice(&[192, 0, 2, 7]);

    // 附加：未知类型 40，原字节 RDATA
    b.push(0); // root owner
    put16(&mut b, 40);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 0);
    put16(&mut b, 6);
    b.extend_from_slice(&[1, 2, 3, 4, 5, 6]);

    b
}

#[test]
fn known_message_parses_with_expected_semantics() {
    let raw = known_response();
    let msg = Message::parse(&raw).expect("已知报文必须解析成功");

    assert_eq!(msg.header.id, 0xABCD);
    assert_eq!(msg.header.qr(), 1);
    assert_eq!(msg.header.rd(), 1);
    assert_eq!(msg.header.ra(), 1);
    assert_eq!(msg.header.rcode(), 0);
    assert_eq!(msg.questions.len(), 1);
    assert_eq!(msg.answers.len(), 2);
    assert_eq!(msg.additional.len(), 1);

    assert_eq!(msg.questions[0].name.to_text_lossy(), "host.example");
    assert_eq!(msg.questions[0].qtype, TYPE_A);

    match &msg.answers[0].rdata {
        Rdata::Cname(n) => assert_eq!(n.to_text_lossy(), "target.example"),
        other => panic!("期望 CNAME，实际 {other:?}"),
    }
    assert_eq!(msg.answers[0].name.to_text_lossy(), "host.example");
    assert_eq!(msg.answers[0].ttl, 60);

    assert_eq!(msg.answers[1].name.to_text_lossy(), "target.example");
    match &msg.answers[1].rdata {
        Rdata::A(a) => assert_eq!(a, &[192, 0, 2, 7]),
        other => panic!("期望 A，实际 {other:?}"),
    }
    assert_eq!(msg.answers[1].ttl, 90);

    // 未知类型保留原字节。
    match &msg.additional[0].rdata {
        Rdata::Unknown { rtype, raw } => {
            assert_eq!(*rtype, 40);
            assert_eq!(raw.as_slice(), &[1, 2, 3, 4, 5, 6]);
        }
        other => panic!("期望 Unknown，实际 {other:?}"),
    }
}

#[test]
fn known_message_roundtrip_decode_encode() {
    let raw = known_response();
    let msg = Message::parse(&raw).unwrap();

    // 语义往返：编码结果必须仍是合法报文，且再次解析后语义完全相同。
    let reencoded = msg.encode().expect("重编码失败");
    let msg2 = Message::parse(&reencoded).expect("重编码结果必须可解析");
    assert_eq!(msg, msg2, "二次解析语义与首次解析不一致");

    // 再编码应幂等。
    let reencoded2 = msg2.encode().unwrap();
    assert_eq!(reencoded, reencoded2, "第二次编码与第一次不一致");

    // 重编码报文必须使用压缩指针（owner 名字复用问题名，故应短于
    // 全量未压缩写法）。
    assert!(
        reencoded.len() <= raw.len(),
        "压缩重编码不应比原始手工报文更长：{} vs {}",
        reencoded.len(),
        raw.len()
    );
}

#[test]
fn aaaa_record_roundtrips() {
    let mut b = Vec::new();
    put16(&mut b, 1);
    put16(&mut b, 0x8000);
    put16(&mut b, 0);
    put16(&mut b, 1);
    put16(&mut b, 0);
    put16(&mut b, 0);
    b.extend_from_slice(&[6, b'v', b'6', b'o', b'n', b'l', b'y', 0]);
    put16(&mut b, TYPE_AAAA);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 123);
    put16(&mut b, 16);
    let addr: [u8; 16] = [
        0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1,
    ];
    b.extend_from_slice(&addr);

    let msg = Message::parse(&b).unwrap();
    match &msg.answers[0].rdata {
        Rdata::Aaaa(a) => assert_eq!(a, &addr),
        other => panic!("实际 {other:?}"),
    }
    let re = msg.encode().unwrap();
    let msg2 = Message::parse(&re).unwrap();
    assert_eq!(msg, msg2);
}

#[test]
fn truncated_record_rddata_is_detected() {
    let mut b = Vec::new();
    put16(&mut b, 1);
    put16(&mut b, 0x0000);
    put16(&mut b, 0);
    put16(&mut b, 1);
    put16(&mut b, 0);
    put16(&mut b, 0);
    b.push(0); // root owner
    put16(&mut b, TYPE_A);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 0);
    put16(&mut b, 4); // RDLENGTH=4，但报文到此结束
    let err = Message::parse(&b).unwrap_err();
    match err {
        DnsError::TruncatedRecord {
            declared, actual, ..
        } => {
            assert_eq!(declared, 4);
            assert_eq!(actual, 0);
        }
        other => panic!("应为 TruncatedRecord，实际 {other:?}"),
    }
}

#[test]
fn truncated_header_is_detected() {
    let err = Message::parse(&[0, 1, 0]).unwrap_err();
    assert!(matches!(err, DnsError::UnexpectedEof { .. }), "{err:?}");
}

#[test]
fn count_exceeding_actual_data_is_detected() {
    // QDCOUNT=2，但只有一个问题。
    let mut b = Vec::new();
    put16(&mut b, 1);
    put16(&mut b, 0x0100);
    put16(&mut b, 2);
    put16(&mut b, 0);
    put16(&mut b, 0);
    put16(&mut b, 0);
    b.extend_from_slice(&[1, b'a', 0]);
    put16(&mut b, TYPE_A);
    put16(&mut b, CLASS_IN);
    let err = Message::parse(&b).unwrap_err();
    assert!(
        matches!(err, DnsError::UnexpectedEof { .. }),
        "{err:?}"
    );
}

#[test]
fn trailing_garbage_is_detected() {
    // QDCOUNT=0，报文头后却多出一个字节。
    let mut b = vec![0u8; 12];
    b[5] = 0;
    b.push(0xFF);
    let err = Message::parse(&b).unwrap_err();
    assert!(matches!(err, DnsError::Framing(..)), "{err:?}");
}

#[test]
fn wrong_rddata_length_for_a_is_detected() {
    let mut b = Vec::new();
    put16(&mut b, 1);
    put16(&mut b, 0x8000);
    put16(&mut b, 0);
    put16(&mut b, 1);
    put16(&mut b, 0);
    put16(&mut b, 0);
    b.push(0);
    put16(&mut b, TYPE_A);
    put16(&mut b, CLASS_IN);
    put32(&mut b, 0);
    put16(&mut b, 3); // A 记录 RDLENGTH 应为 4
    b.extend_from_slice(&[1, 2, 3]);
    let err = Message::parse(&b).unwrap_err();
    assert!(matches!(err, DnsError::TruncatedRecord { .. }), "{err:?}");
}

#[test]
fn encoded_message_uses_compression_pointer() {
    // 问题与回答同名时，回答 owner 必须编码为 2 字节指针。
    let raw = {
        let mut b = Vec::new();
        put16(&mut b, 1);
        put16(&mut b, 0x8180);
        put16(&mut b, 1);
        put16(&mut b, 1);
        put16(&mut b, 0);
        put16(&mut b, 0);
        b.extend_from_slice(&[9]);
        b.extend_from_slice(b"long-name");
        b.extend_from_slice(&[7]);
        b.extend_from_slice(b"example");
        b.extend_from_slice(&[3]);
        b.extend_from_slice(b"com");
        b.push(0);
        put16(&mut b, TYPE_A);
        put16(&mut b, CLASS_IN);
        // owner 指针
        put16(&mut b, 0xC00C);
        put16(&mut b, TYPE_A);
        put16(&mut b, CLASS_IN);
        put32(&mut b, 10);
        put16(&mut b, 4);
        b.extend_from_slice(&[1, 2, 3, 4]);
        b
    };
    let msg = Message::parse(&raw).unwrap();
    let re = msg.encode().unwrap();
    // 找到回答段 owner：应包含 0xC0 0x0C。
    assert!(
        re.windows(2).any(|w| w == [0xC0, 0x0C]),
        "重编码报文未包含指向问题名的压缩指针：{re:02x?}"
    );
}
