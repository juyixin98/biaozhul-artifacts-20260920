//! 核心解码/编码验收测试：
//! - 前向指针、越界标签、指针环、截断资源记录；
//! - 保留/扩展标签前缀、截断指针、指针链上限（自定义上限）；
//! - 已知报文的解码→编码语义往返（含未知类型字节保留、大小写不敏感比较）。

use dns_compress::message::{Flags, Message, Rdata, ResourceRecord, CLASS_IN, TYPE_A};
use dns_compress::parser::Limits;
use dns_compress::DnsError;

fn hex(h: &str) -> Vec<u8> {
    let s: String = h.chars().filter(|c| !c.is_whitespace()).collect();
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn hdr(id: u16, flags: u16, qd: u16, an: u16, ns: u16, ar: u16) -> Vec<u8> {
    let mut v = Vec::with_capacity(12);
    for x in [id, flags, qd, an, ns, ar] {
        v.extend_from_slice(&x.to_be_bytes());
    }
    v
}

/// 标准 A 查询（无压缩）。
fn query_a() -> Vec<u8> {
    let mut b = hdr(0x1234, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&hex("03 77 77 77 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&0x0001u16.to_be_bytes()); // TYPE A
    b.extend_from_slice(&0x0001u16.to_be_bytes()); // CLASS IN
    b
}

/// 带 CNAME 压缩的响应（名字→12，目标 example.com→16）。
fn response_cname_compressed() -> Vec<u8> {
    let mut b = hdr(0x9abc, 0x8180, 1, 1, 0, 0);
    b.extend_from_slice(&hex("03 77 77 77 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&0x0005u16.to_be_bytes()); // CNAME
    b.extend_from_slice(&0x0001u16.to_be_bytes());
    // RR: NAME 指针→12, CNAME, IN, 300, RDLEN=8, "alias" 指针→16
    b.extend_from_slice(&hex(
        "c0 0c 00 05 00 01 00 00 01 2c 00 08 05 61 6c 69 61 73 c0 10",
    ));
    b
}

#[test]
fn known_a_query_decodes() {
    let msg = Message::parse(&query_a(), &Limits::default()).unwrap();
    assert_eq!(msg.id, 0x1234);
    assert_eq!(msg.questions.len(), 1);
    let q = &msg.questions[0];
    assert_eq!(q.qtype, TYPE_A);
    assert_eq!(q.qclass, CLASS_IN);
    assert_eq!(q.qname.to_string(), "www.example.com.");
    assert!(msg.answers.is_empty());
    assert!(msg.flags.rd);
    assert!(!msg.flags.qr);
}

#[test]
fn uncompressed_query_is_byte_identical_after_roundtrip() {
    let raw = query_a();
    let msg = Message::parse(&raw, &Limits::default()).unwrap();
    let again = msg.encode(&Limits::default()).unwrap();
    assert_eq!(raw, again, "无压缩报文往返后必须逐字节一致");
}

#[test]
fn compressed_cname_response_decodes() {
    let msg = Message::parse(&response_cname_compressed(), &Limits::default()).unwrap();
    assert_eq!(msg.questions[0].qname.to_string(), "www.example.com.");
    assert_eq!(msg.answers.len(), 1);
    let rr = &msg.answers[0];
    assert_eq!(rr.name.to_string(), "www.example.com.");
    assert_eq!(rr.ttl, 300);
    match &rr.rdata {
        Rdata::Cname(n) => assert_eq!(n.to_string(), "alias.example.com."),
        other => panic!("期望 CNAME，得到 {other:?}"),
    }
}

#[test]
fn semantic_roundtrip_of_compressed_response() {
    let raw = response_cname_compressed();
    let msg = Message::parse(&raw, &Limits::default()).unwrap();
    let reencoded = msg.encode(&Limits::default()).unwrap();
    // 非压缩重编码必然更长但语义等价。
    assert!(reencoded.len() > raw.len());
    let msg2 = Message::parse(&reencoded, &Limits::default()).unwrap();
    assert_eq!(msg, msg2);
    assert_eq!(
        msg2.answers[0].rdata,
        Rdata::Cname(dns_compress::Name::from_dotted("alias.example.com").unwrap())
    );
}

/// 前向指针：QNAME 的 "www" 后面跟一个指向后续 RDATA 中 "example.com" 的指针。
/// 报文是一个对 A 查询返回 CNAME 应答的常见形态（CNAME 的 RDATA 是被指向的名字）。
#[test]
fn forward_pointer_is_followed() {
    let mut b = hdr(0xf00d, 0x8180, 1, 1, 0, 0);
    b.push(3);
    b.extend_from_slice(b"www");
    let fwd_pos = b.len();
    b.extend_from_slice(&[0, 0]); // 前向指针占位
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // RR name →12（www...）
    b.extend_from_slice(&5u16.to_be_bytes()); // TYPE=CNAME
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    let rdata_target = b.len() + 2; // RDLENGTH 之后即名字
    let rdlen = 1 + 7 + 1 + 3 + 1; // example.com 的在线长度 13
    b.extend_from_slice(&(rdlen as u16).to_be_bytes());
    b.extend_from_slice(&hex("07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    let ptr = 0xC000 | rdata_target as u16;
    b[fwd_pos] = (ptr >> 8) as u8;
    b[fwd_pos + 1] = (ptr & 0xFF) as u8;

    let msg = Message::parse(&b, &Limits::default()).unwrap();
    assert_eq!(msg.questions[0].qname.to_string(), "www.example.com.");
    // 语义往返
    let again = msg.encode(&Limits::default()).unwrap();
    let msg2 = Message::parse(&again, &Limits::default()).unwrap();
    assert_eq!(msg.questions, msg2.questions);
    match &msg2.answers[0].rdata {
        Rdata::Cname(n) => assert_eq!(n.to_string(), "example.com."),
        other => panic!("{other:?}"),
    }
}

#[test]
fn out_of_bounds_label() {
    let mut b = hdr(0xbad, 0x0100, 1, 0, 0, 0);
    b.push(10);
    b.extend_from_slice(b"ab");
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::UnexpectedEof)
    );
}

#[test]
fn pointer_self_loop() {
    let mut b = hdr(0xbad, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // 指向自身 12
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::PointerLoop)
    );
}

#[test]
fn pointer_pair_loop() {
    let mut b = hdr(0xbad, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&0xC00Eu16.to_be_bytes()); // 12→14
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // 14→12
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::PointerLoop)
    );
}

#[test]
fn pointer_out_of_bounds() {
    let mut b = hdr(0xbad, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&0xC0FFu16.to_be_bytes()); // 指向 255，远超报文长度
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::PointerOutOfBounds)
    );
}

#[test]
fn truncated_pointer() {
    let mut b = hdr(0xbad, 0x0100, 1, 0, 0, 0);
    b.push(0xC0); // 指针首字节后报文结束
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::TruncatedPointer)
    );
}

#[test]
fn truncated_resource_record() {
    let mut b = hdr(0xbad, 0x8180, 1, 1, 0, 0);
    b.extend_from_slice(&hex("07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&8u16.to_be_bytes());
    b.extend_from_slice(&[192, 0, 2, 1]);
    match Message::parse(&b, &Limits::default()) {
        Err(DnsError::RdataLengthMismatch {
            declared: 8,
            available: 4,
        }) => {}
        other => panic!("期望 RdataLengthMismatch，得到 {other:?}"),
    }
}

/// CNAME 的名字物理消耗必须恰好等于 RDLENGTH。
#[test]
fn cname_name_crossing_rdlength_rejected() {
    // RDLENGTH 故意少写 1（真实名字 "\x01a\x00" 占 3 字节，声明 2），
    // 后面再放一个字节保证名字读得到 0 —— 没有边界检查就会吞掉下一条记录的字节。
    let mut b = hdr(1, 0x8180, 0, 1, 0, 0);
    b.extend_from_slice(&hex("01 62 00")); // 属主名 "b"
    b.extend_from_slice(&5u16.to_be_bytes()); // CNAME
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&2u16.to_be_bytes()); // RDLENGTH=2（应为 3）
    b.extend_from_slice(&hex("01 61 00")); // RDATA 位置上是完整 3 字节名字 "a"
    assert!(matches!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::RdataLengthMismatch {
            declared: 2,
            available: 3
        })
    ));

    // RDLENGTH 多写 1（名字后有多余尾巴）同样拒绝。
    let mut b2 = hdr(1, 0x8180, 0, 1, 0, 0);
    b2.extend_from_slice(&hex("01 62 00"));
    b2.extend_from_slice(&5u16.to_be_bytes());
    b2.extend_from_slice(&CLASS_IN.to_be_bytes());
    b2.extend_from_slice(&60u32.to_be_bytes());
    b2.extend_from_slice(&4u16.to_be_bytes()); // RDLENGTH=4（应为 3）
    b2.extend_from_slice(&hex("01 61 00 ff"));
    assert!(matches!(
        Message::parse(&b2, &Limits::default()),
        Err(DnsError::RdataLengthMismatch {
            declared: 4,
            available: 3
        })
    ));
}

#[test]
fn malformed_a_rdata_length_rejected() {
    // 类型为 A 但 RDLENGTH=3（名字用非压缩形式，避免先触发指针错误）
    let mut b = hdr(1, 0x8180, 0, 1, 0, 0);
    b.extend_from_slice(&hex("01 61 00"));
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&3u16.to_be_bytes());
    b.extend_from_slice(&[1, 2, 3]);
    assert!(matches!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::InvalidRdataLen {
            rtype: 1,
            declared: 3,
            expected: 4
        })
    ));
}

#[test]
fn reserved_label_prefix_rejected() {
    let mut b = hdr(1, 0x0100, 1, 0, 0, 0);
    b.push(0x40);
    b.extend_from_slice(b"xx");
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::ReservedLabelKind)
    );
}

#[test]
fn extended_label_prefix_rejected() {
    let mut b = hdr(1, 0x0100, 1, 0, 0, 0);
    b.push(0x80);
    b.extend_from_slice(b"xx");
    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::UnsupportedExtendedLabel)
    );
}

#[test]
fn unknown_rdata_preserved_as_bytes() {
    let rdata = b"v=spf1 -all";

    // 变体一：属主名使用压缩指针；整包重编码不必逐字节相同（输出不压缩），
    // 但未知类型的 RDATA 必须原样保留、整包语义必须往返。
    let mut b = hdr(0x7777, 0x8180, 1, 1, 0, 0);
    b.extend_from_slice(&hex("07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b.extend_from_slice(&99u16.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&0xC00Cu16.to_be_bytes());
    b.extend_from_slice(&99u16.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&300u32.to_be_bytes());
    b.extend_from_slice(&(rdata.len() as u16).to_be_bytes());
    b.extend_from_slice(rdata);

    let msg = Message::parse(&b, &Limits::default()).unwrap();
    assert!(
        matches!(&msg.answers[0].rdata, Rdata::Unknown(99, bytes) if bytes.as_slice() == rdata)
    );
    let again = msg.encode(&Limits::default()).unwrap();
    let msg2 = Message::parse(&again, &Limits::default()).unwrap();
    assert_eq!(msg2, msg, "语义往返一致");
    assert!(
        matches!(&msg2.answers[0].rdata, Rdata::Unknown(99, bytes) if bytes.as_slice() == rdata)
    );

    // 变体二：属主名也不压缩 → 整包逐字节往返。
    let mut b2 = hdr(0x7778, 0x8180, 0, 1, 0, 0);
    b2.extend_from_slice(&hex("07 65 78 61 6d 70 6c 65 03 63 6f 6d 00"));
    b2.extend_from_slice(&99u16.to_be_bytes());
    b2.extend_from_slice(&CLASS_IN.to_be_bytes());
    b2.extend_from_slice(&300u32.to_be_bytes());
    b2.extend_from_slice(&(rdata.len() as u16).to_be_bytes());
    b2.extend_from_slice(rdata);
    let msg3 = Message::parse(&b2, &Limits::default()).unwrap();
    assert_eq!(msg3.encode(&Limits::default()).unwrap(), b2);
}

#[test]
fn pointer_chain_limit_is_enforced_and_configurable() {
    // RR 在前、指针链槽位在后、根零字节在最后。
    // RR：NAME 指针（占位，指向最深槽位）+ A/IN/60/RDLEN4/192.0.2.1
    let mut b = hdr(0xbeef, 0x8180, 0, 1, 0, 0);
    let name_ptr_pos = b.len();
    b.extend_from_slice(&[0, 0]); // 最深槽位指针占位
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    b.extend_from_slice(&4u16.to_be_bytes());
    b.extend_from_slice(&[192, 0, 2, 1]);

    let chain_start = b.len();
    for i in 0..70u16 {
        // slot i 位于 chain_start+2i：slot0 指向链后的根，其余指向前一个槽。
        let target = if i == 0 {
            chain_start + 70 * 2
        } else {
            chain_start + 2 * (i - 1) as usize
        };
        b.extend_from_slice(&(0xC000u16 | target as u16).to_be_bytes());
    }
    b.push(0); // 根
    let deepest = chain_start + 2 * 69;
    let ptr = 0xC000u16 | deepest as u16;
    b[name_ptr_pos] = (ptr >> 8) as u8;
    b[name_ptr_pos + 1] = (ptr & 0xFF) as u8;

    assert_eq!(
        Message::parse(&b, &Limits::default()),
        Err(DnsError::PointerChainTooLong),
        "默认上限 64 应拒绝 71 跳链"
    );

    let loose = Limits {
        max_pointer_jumps: 128,
        ..Limits::default()
    };
    let msg = Message::parse(&b, &loose).expect("上限 128 时应能解码");
    // 链最终终止于根标签 → 属主名是根。
    assert!(msg.answers[0].name.is_root());
}

#[test]
fn name_length_limits_enforced() {
    // 127 个单字符标签：线长度 1 + 127*2 = 255（恰好等于协议上限）→ 通过。
    let mut ok_name = Vec::new();
    for _ in 0..127 {
        ok_name.push(1);
        ok_name.push(b'a');
    }
    ok_name.push(0);
    assert_eq!(ok_name.len(), 255);
    let mut pkt_ok = hdr(1, 0x0100, 1, 0, 0, 0);
    pkt_ok.extend_from_slice(&ok_name);
    pkt_ok.extend_from_slice(&TYPE_A.to_be_bytes());
    pkt_ok.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert!(Message::parse(&pkt_ok, &Limits::default()).is_ok());

    // 128 个标签 → 257 字节 → NameTooLong。
    let mut bad_name = ok_name;
    bad_name.pop(); // 去根
    bad_name.push(1);
    bad_name.push(b'b');
    bad_name.push(0);
    assert_eq!(bad_name.len(), 257);
    let mut pkt_bad = hdr(1, 0x0100, 1, 0, 0, 0);
    pkt_bad.extend_from_slice(&bad_name);
    pkt_bad.extend_from_slice(&TYPE_A.to_be_bytes());
    pkt_bad.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(
        Message::parse(&pkt_bad, &Limits::default()),
        Err(DnsError::NameTooLong)
    );

    // 自定义标签数上限。
    let tight = Limits {
        max_labels: 1,
        ..Limits::default()
    };
    let mut b = hdr(1, 0x0100, 1, 0, 0, 0);
    b.extend_from_slice(&hex("01 61 01 62 00")); // a.b（2 个标签）
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(Message::parse(&b, &tight), Err(DnsError::TooManyLabels));
}

/// 压缩名字的 255 上限按**展开后**长度计算：多个指针片段拼成的合法名字
/// 不应因为指针自身的两字节被计入而误杀。
#[test]
fn compressed_name_length_measures_expanded_form() {
    // 报文（an=1）：
    //   偏移 12 的 QNAME = "\x3f(63个c)" + **前向指针**（占位，指向后续 RDATA）
    //   偏移 82 的 Answer RR：NAME 回指 QNAME（→12），CNAME，
    //   RDATA（偏移 94）= "\x3f(63个a)\x01b\x00" —— 前向指针的目标。
    // 展开后 QNAME 为 c/a/b 三个标签，wire_len = 64+64+2+1 = 131。
    let mut b = hdr(1, 0x8180, 1, 1, 0, 0);
    assert_eq!(b.len(), 12);
    b.push(63);
    b.extend(std::iter::repeat_n(b'c', 63));
    assert_eq!(b.len(), 76);
    let fwd_pos = b.len();
    b.extend_from_slice(&[0, 0]); // 前向指针占位（76-77）
    b.extend_from_slice(&TYPE_A.to_be_bytes()); // 78-79
    b.extend_from_slice(&CLASS_IN.to_be_bytes()); // 80-81
    assert_eq!(b.len(), 82);
    b.extend_from_slice(&0xC00Cu16.to_be_bytes()); // RR NAME →12
    b.extend_from_slice(&5u16.to_be_bytes()); // TYPE=CNAME
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&60u32.to_be_bytes());
    assert_eq!(b.len(), 92);
    let rdata_target = b.len() + 2; // 跳过 RDLENGTH
    let rdlen = 64 + 2 + 1; // 3f+63a + 01 62 + 00
    b.extend_from_slice(&(rdlen as u16).to_be_bytes());
    assert_eq!(b.len(), rdata_target);
    b.push(63);
    b.extend(std::iter::repeat_n(b'a', 63));
    b.extend_from_slice(&hex("01 62 00"));
    let ptr = 0xC000u16 | rdata_target as u16;
    b[fwd_pos] = (ptr >> 8) as u8;
    b[fwd_pos + 1] = (ptr & 0xFF) as u8;

    let msg = Message::parse(&b, &Limits::default()).unwrap();
    let n = &msg.questions[0].qname;
    assert_eq!(n.label_count(), 3);
    assert_eq!(n.label(2), Some(b"b".as_ref()));
    assert_eq!(n.wire_len(), 131);
    assert!(matches!(&msg.answers[0].rdata, Rdata::Cname(c) if c.label_count() == 2));

    // 边界：展开后恰好 255 字节（127 个标签）的压缩名字合法。
    // QNAME = 126 个 "y" 标签 + root（253 字节，从偏移 12 开始）；
    // Answer RR 的 CNAME RDATA = "\x01z" + 回指指针→12，
    // 展开后 CNAME 目标为 z + 126 个 y = 127 个标签、wire_len=255。
    let mut b2 = hdr(2, 0x8180, 1, 1, 0, 0);
    for _ in 0..126 {
        b2.extend_from_slice(&hex("01 79"));
    }
    b2.push(0); // QNAME 结束（12..265）
    b2.extend_from_slice(&5u16.to_be_bytes()); // QTYPE=CNAME
    b2.extend_from_slice(&CLASS_IN.to_be_bytes());
    // RR：NAME→12，CNAME，IN，60，RDLEN=4（"\x01z" 2B + 指针 2B）
    b2.extend_from_slice(&0xC00Cu16.to_be_bytes());
    b2.extend_from_slice(&5u16.to_be_bytes());
    b2.extend_from_slice(&CLASS_IN.to_be_bytes());
    b2.extend_from_slice(&60u32.to_be_bytes());
    b2.extend_from_slice(&4u16.to_be_bytes());
    b2.extend_from_slice(&hex("01 7a"));
    b2.extend_from_slice(&0xC00Cu16.to_be_bytes()); // →QNAME 起始
    let msg2 = Message::parse(&b2, &Limits::default()).unwrap();
    let target = match &msg2.answers[0].rdata {
        Rdata::Cname(n) => n,
        other => panic!("{other:?}"),
    };
    assert_eq!(target.label_count(), 127);
    assert_eq!(target.wire_len(), 255);
}

#[test]
fn section_record_cap_enforced() {
    let tight = Limits {
        max_records_per_section: 1,
        ..Limits::default()
    };
    // qd=2，仅给一个完整问题即触发上限。
    let mut b = hdr(1, 0x0100, 2, 0, 0, 0);
    b.extend_from_slice(&hex("01 61 00"));
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    assert_eq!(
        Message::parse(&b, &tight),
        Err(DnsError::TooManyRecords(
            dns_compress::error::Section::Question
        ))
    );
}

#[test]
fn message_too_short() {
    assert_eq!(
        Message::parse(&[0, 1, 2], &Limits::default()),
        Err(DnsError::MessageTooShort)
    );
}

#[test]
fn case_insensitive_name_compare_preserves_bytes() {
    let upper = dns_compress::Name::from_dotted("WWW.EXAMPLE.COM").unwrap();
    let lower = dns_compress::Name::from_dotted("www.example.com.").unwrap();
    assert_ne!(upper, lower, "结构化比较保留原始大小写");
    assert!(upper.eq_ignore_ascii_case(&lower));
    // 编码保留字节（不做大小写折叠）。
    let mut pkt_upper = hdr(1, 0x0100, 1, 0, 0, 0);
    pkt_upper.extend_from_slice(&upper.encode());
    pkt_upper.extend_from_slice(&TYPE_A.to_be_bytes());
    pkt_upper.extend_from_slice(&CLASS_IN.to_be_bytes());
    let msg = Message::parse(&pkt_upper, &Limits::default()).unwrap();
    assert_eq!(msg.questions[0].qname, upper);
}

#[test]
fn flags_roundtrip_preserves_z() {
    let mut b = hdr(1, 0x0170, 1, 0, 0, 0); // rd + z=7
    b.extend_from_slice(&hex("01 61 00"));
    b.extend_from_slice(&TYPE_A.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    let msg = Message::parse(&b, &Limits::default()).unwrap();
    assert_eq!(msg.flags.z, 7);
    assert!(msg.flags.rd);
    let again = msg.encode(&Limits::default()).unwrap();
    assert_eq!(again, b);
}

#[test]
fn aaaa_record_roundtrip() {
    let mut b = hdr(1, 0x8180, 0, 1, 0, 0);
    b.extend_from_slice(&hex("01 61 00")); // name "a"
    b.extend_from_slice(&28u16.to_be_bytes());
    b.extend_from_slice(&CLASS_IN.to_be_bytes());
    b.extend_from_slice(&120u32.to_be_bytes());
    b.extend_from_slice(&16u16.to_be_bytes());
    b.extend_from_slice(&hex("20 01 0d b8 00 00 00 00 00 00 00 00 00 00 00 01"));
    let msg = Message::parse(&b, &Limits::default()).unwrap();
    match &msg.answers[0].rdata {
        Rdata::Aaaa(addr) => assert_eq!(addr.to_string(), "2001:db8::1"),
        other => panic!("{other:?}"),
    }
    let again = msg.encode(&Limits::default()).unwrap();
    assert_eq!(again, b);
}

#[test]
fn constructed_message_end_to_end() {
    let msg = Message {
        id: 0x4242,
        flags: Flags {
            qr: true,
            rd: true,
            ra: true,
            ..Flags::default()
        },
        questions: vec![dns_compress::message::Question {
            qname: dns_compress::Name::from_dotted("claude.ai").unwrap(),
            qtype: TYPE_A,
            qclass: CLASS_IN,
        }],
        answers: vec![ResourceRecord {
            name: dns_compress::Name::from_dotted("claude.ai").unwrap(),
            rtype: TYPE_A,
            rclass: CLASS_IN,
            ttl: 1,
            rdata: Rdata::A("1.2.3.4".parse().unwrap()),
        }],
        authorities: vec![],
        additionals: vec![],
    };
    let wire = msg.encode(&Limits::default()).unwrap();
    let back = Message::parse(&wire, &Limits::default()).unwrap();
    assert_eq!(back, msg);
}
