//! 名字压缩解压单元测试：正常指针、前向指针、越界标签、
//! 指针环、跳转上限、保留标签类型、长度上限。

use dns_compress::name::{Name, MAX_POINTER_JUMPS};
use dns_compress::reader::Reader;
use dns_compress::DnsError;

/// 从绝对偏移 12 开始解析名字。
fn parse_from_12(bytes: &[u8]) -> Result<(Name, usize), DnsError> {
    let mut r = Reader::new(bytes);
    r.seek_to(12).unwrap();
    Name::parse(&mut r)
}

fn header(qd: u16, an: u16, ns: u16, ar: u16) -> Vec<u8> {
    let mut m = Vec::new();
    for v in [0x1234u16, 0x0100, qd, an, ns, ar] {
        m.extend_from_slice(&v.to_be_bytes());
    }
    m
}

#[test]
fn parses_uncompressed_name() {
    let mut b = header(1, 0, 0, 0);
    b.extend_from_slice(&[3, b'w', b'w', b'w', 7, b'e', b'x', b'a', b'm', b'p', b'l', b'e', 3, b'c', b'o', b'm', 0]);
    let (n, consumed) = parse_from_12(&b).unwrap();
    assert_eq!(n.to_text_lossy(), "www.example.com");
    assert_eq!(n.label_count(), 3);
    assert_eq!(consumed, 17); // 4+8+4+1
}

#[test]
fn parses_root_name() {
    let mut b = header(1, 0, 0, 0);
    b.push(0);
    let (n, consumed) = parse_from_12(&b).unwrap();
    assert!(n.is_root());
    assert_eq!(consumed, 1);
}

#[test]
fn parses_backward_pointer_to_question() {
    // 经典应答：问题在 12，回答 owner 用指针指回 12。
    let mut b = header(1, 1, 0, 0);
    // 问题名 host：[4]host[0] 共 6 字节（偏移 12..18）。
    b.extend_from_slice(&[4, b'h', b'o', b's', b't', 0]);
    b.extend_from_slice(&1u16.to_be_bytes()); // 18..20
    b.extend_from_slice(&1u16.to_be_bytes()); // 20..22
    // RR owner 指针位于偏移 22 -> 12，名字结束于 24。
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());
    assert_eq!(b.len(), 24);
    let mut r = Reader::new(&b);
    r.seek_to(22).unwrap();
    let (n, consumed) = Name::parse(&mut r).unwrap();
    assert_eq!(n.to_text_lossy(), "host");
    assert_eq!(consumed, 2);
    assert_eq!(r.position(), 24);
}

#[test]
fn parses_forward_pointer_into_later_rdata() {
    // 问题名（偏移12）用前向指针指向附加段未知 RR 的 RDATA 中部（偏移41）。
    let mut b = header(1, 0, 0, 1);
    assert_eq!(b.len(), 12);
    // 问题：指针 -> 41，然后 QTYPE/QCLASS
    b.extend_from_slice(&[0xC0, 0x29]);
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    assert_eq!(b.len(), 18);
    // 附加 RR：root owner + TYPE99/CLASS/TTL/RDLENGTH
    b.push(0);
    b.extend_from_slice(&99u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&0u32.to_be_bytes());
    b.extend_from_slice(&18u16.to_be_bytes());
    assert_eq!(b.len(), 29);
    b.extend_from_slice(&[0xABu8; 12]);
    assert_eq!(b.len(), 41);
    b.extend_from_slice(&[4, b'h', b'o', b's', b't', 0]);
    assert_eq!(b.len(), 47);

    // 整体解析必须成功（前向指针被明确支持）。
    let msg = dns_compress::Message::parse(&b).expect("含前向指针的报文应解析成功");
    assert_eq!(msg.questions[0].name.to_text_lossy(), "host");
    match &msg.additional[0].rdata {
        dns_compress::Rdata::Unknown { rtype, raw } => {
            assert_eq!(*rtype, 99);
            assert_eq!(raw.len(), 18);
        }
        other => panic!("应为 Unknown，实际 {other:?}"),
    }
}

#[test]
fn pointer_out_of_bounds_is_detected() {
    let mut b = header(1, 0, 0, 0);
    b.extend_from_slice(&[0xC1, 0x00]); // 指针 -> 256
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    let err = parse_from_12(&b).unwrap_err();
    match err {
        DnsError::PointerOutOfBounds { target, msg_len } => {
            assert_eq!(target, 256);
            assert_eq!(msg_len, b.len());
        }
        other => panic!("应为 PointerOutOfBounds，实际 {other:?}"),
    }
}

#[test]
fn pointer_loop_is_detected() {
    let mut b = header(1, 0, 0, 0);
    b.extend_from_slice(&[0xC0, 0x0C]); // 指针 -> 自身偏移 12
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    let err = parse_from_12(&b).unwrap_err();
    assert!(
        matches!(err, DnsError::PointerLoop { offset: 12 }),
        "实际错误：{err:?}"
    );
}

#[test]
fn two_node_pointer_loop_is_detected() {
    // 偏移 12 -> 14，偏移 14 -> 12，形成两节点环。
    let mut b = header(1, 0, 0, 0);
    b.extend_from_slice(&[0xC0, 0x0E]); // 12 -> 14
    b.extend_from_slice(&[0xC0, 0x0C]); // 14 -> 12
    let err = parse_from_12(&b).unwrap_err();
    assert!(matches!(err, DnsError::PointerLoop { .. }), "实际：{err:?}");
}

#[test]
fn pointer_jump_cap_is_enforced() {
    // 构造一条无环但每次指针都跳到新位置的链，超过 MAX_POINTER_JUMPS。
    // 每个跳转目标放另一个指针，形成 N 个互不重复的偏移，
    // 最后一个目标才放一个真正标签——但跳转数先超限。
    let mut b = header(1, 0, 0, 0);
    // 起始 12 -> 14 -> 16 -> ...（每节点 2 字节）
    let nodes = MAX_POINTER_JUMPS + 5;
    for i in 0..nodes {
        let here = 12 + 2 * i;
        let target = here + 2;
        b.push(0xC0 | ((target >> 8) as u8));
        b.push((target & 0xFF) as u8);
    }
    // 链尾放越界内容也没关系：跳转上限应先触发。
    let err = parse_from_12(&b).unwrap_err();
    assert!(
        matches!(err, DnsError::TooManyPointers { .. }),
        "实际：{err:?}"
    );
}

#[test]
fn out_of_bounds_label_is_detected() {
    // 标签声称 60 字节，实际只剩 3 字节。
    let mut b = header(1, 0, 0, 0);
    b.push(60);
    b.extend_from_slice(b"abc");
    let err = parse_from_12(&b).unwrap_err();
    assert!(
        matches!(err, DnsError::UnexpectedEof { .. }),
        "实际：{err:?}"
    );
}

#[test]
fn truncated_zero_length_terminator_is_detected() {
    // 只有一个标签长度字节，标签内容完全缺失。
    let mut b = header(1, 0, 0, 0);
    b.push(1);
    let err = parse_from_12(&b).unwrap_err();
    assert!(matches!(err, DnsError::UnexpectedEof { .. }));
}

#[test]
fn reserved_label_types_are_rejected() {
    let mut b = header(1, 0, 0, 0);
    b.push(0x40); // 0b01 前缀
    b.push(0x00);
    let err = parse_from_12(&b).unwrap_err();
    assert!(matches!(err, DnsError::UnsupportedLabelType { prefix: 0b01 }));

    let mut b = header(1, 0, 0, 0);
    b.push(0x80); // 0b10 前缀
    b.push(0x00);
    let err = parse_from_12(&b).unwrap_err();
    assert!(matches!(err, DnsError::UnsupportedLabelType { prefix: 0b10 }));
}

#[test]
fn name_longer_than_255_is_rejected() {
    // 36 个长度为 7 的标签 => 8 + 36*7 = 260 > 255（含结尾 0）。
    let mut b = header(1, 0, 0, 0);
    for _ in 0..36 {
        b.push(7);
        b.extend_from_slice(b"aaaaaaa");
    }
    b.push(0);
    let err = parse_from_12(&b).unwrap_err();
    assert!(matches!(err, DnsError::LabelTooLong { .. }), "实际：{err:?}");
}

#[test]
fn name_of_exactly_255_wire_bytes_is_accepted() {
    // 线长恰好 255：可用 31 个 7 字节标签 => 1 + 31*8 = 249，再加一个
    // 5 字节标签：1+5 => 总 255（结尾 0 已计入最后那个 +1？）。
    // 计算：每标签开销 = 1 长度 + L 内容；结尾另有 1 字节。
    // 31*8 = 248，结尾 0 = 249；再放 5 字节标签 = 6 => 255。
    let mut b = header(1, 0, 0, 0);
    for _ in 0..31 {
        b.push(7);
        b.extend_from_slice(b"aaaaaaa");
    }
    b.push(5);
    b.extend_from_slice(b"aaaaa");
    b.push(0);
    let (n, _) = parse_from_12(&b).unwrap();
    assert_eq!(n.wire_len(), 255);
}

#[test]
fn cname_pointer_in_rdata_can_target_message_header_area() {
    // CNAME 的 RDATA 中使用指针指向问题名字（比 RR 更早的位置）。
    let mut b = header(1, 1, 0, 0);
    b.extend_from_slice(&[5, b'a', b'l', b'i', b'a', b's', 0]); // 偏移12 alias. 7字节
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes()); // 问题结束于 21
    // RR owner = root，TYPE=CNAME, CLASS=IN, TTL=0, RDLENGTH=2，RDATA=指针->12
    b.push(0);
    b.extend_from_slice(&5u16.to_be_bytes());
    b.extend_from_slice(&1u16.to_be_bytes());
    b.extend_from_slice(&0u32.to_be_bytes());
    b.extend_from_slice(&2u16.to_be_bytes());
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());

    let msg = dns_compress::Message::parse(&b).unwrap();
    match &msg.answers[0].rdata {
        dns_compress::Rdata::Cname(n) => assert_eq!(n.to_text_lossy(), "alias"),
        other => panic!("实际：{other:?}"),
    }
}
