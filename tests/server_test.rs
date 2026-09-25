//! TCP 服务端到端测试：真实 TcpListener + 线程内服务，
//! 覆盖 A/AAAA/CNAME 查询、NXDOMAIN、不支持类型、FORMERR（畸形帧载荷）。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use dns_compress::message::{Message, CLASS_IN, TYPE_A, TYPE_AAAA, TYPE_CNAME};
use dns_compress::name::Name;
use dns_compress::server::Zone;

fn spawn_server() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let zone = Zone::demo();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let s = stream.unwrap();
            let z = zone.clone();
            std::thread::spawn(move || {
                let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
                let (r, w) = (&s, &s);
                let _ = dns_compress::server::serve_connection(r, w, &z);
            });
        }
    });
    port
}

fn connect(port: u16) -> TcpStream {
    let s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    s.set_nodelay(true).unwrap();
    s
}

fn frame(msg: &[u8]) -> Vec<u8> {
    let mut v = Vec::with_capacity(msg.len() + 2);
    v.extend_from_slice(&(msg.len() as u16).to_be_bytes());
    v.extend_from_slice(msg);
    v
}

fn round(s: &mut TcpStream, msg: &[u8]) -> Vec<u8> {
    s.write_all(&frame(msg)).unwrap();
    s.flush().unwrap();
    let mut lb = [0u8; 2];
    s.read_exact(&mut lb).unwrap();
    let n = u16::from_be_bytes(lb) as usize;
    let mut buf = vec![0u8; n];
    s.read_exact(&mut buf).unwrap();
    buf
}

fn query(name: &str, qtype: u16, id: u16) -> Vec<u8> {
    let m = Message {
        header: dns_compress::message::Header {
            id,
            flags: dns_compress::message::Header::build_flags(0, 0, 0, 0, 1, 0, 0, 0),
            qdcount: 1,
            ..Default::default()
        },
        questions: vec![dns_compress::message::Question {
            name: Name::from_text(name).unwrap(),
            qtype,
            qclass: CLASS_IN,
        }],
        ..Default::default()
    };
    m.encode().unwrap()
}

#[test]
fn a_query_returns_localhost_address() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("localhost.test", TYPE_A, 0x7777));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.id, 0x7777);
    assert_eq!(msg.header.qr(), 1);
    assert_eq!(msg.header.rcode(), 0);
    assert!(msg.answers.iter().any(|r| matches!(
        r.rdata,
        dns_compress::Rdata::A([127, 0, 0, 1])
    )));
    // 应答本身可再编码并保持语义。
    let re = msg.encode().unwrap();
    assert_eq!(Message::parse(&re).unwrap(), msg);
}

#[test]
fn aaaa_query_returns_ipv6_loopback() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("localhost.test", TYPE_AAAA, 1));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), 0);
    let mut expected = [0u8; 16];
    expected[15] = 1;
    assert!(msg
        .answers
        .iter()
        .any(|r| matches!(r.rdata, dns_compress::Rdata::Aaaa(a) if a == expected)));
}

#[test]
fn cname_chain_query_follows_alias() {
    let port = spawn_server();
    let mut s = connect(port);
    // A 查询 www.example.test -> CNAME cdn.example.test -> A 192.0.2.10
    let resp_bytes = round(&mut s, &query("www.example.test", TYPE_A, 2));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), 0);
    let has_cname = msg.answers.iter().any(|r| {
        matches!(&r.rdata, dns_compress::Rdata::Cname(n)
            if n.to_text_lossy() == "cdn.example.test")
    });
    let has_a = msg
        .answers
        .iter()
        .any(|r| matches!(r.rdata, dns_compress::Rdata::A([192, 0, 2, 10])));
    assert!(has_cname, "缺少 CNAME 记录：{msg:?}");
    assert!(has_a, "缺少链尾 A 记录：{msg:?}");
}

#[test]
fn cname_query_returns_the_cname_record() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("www.example.test", TYPE_CNAME, 3));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), 0);
    assert!(msg.answers.iter().any(|r| matches!(
        &r.rdata,
        dns_compress::Rdata::Cname(n) if n.to_text_lossy() == "cdn.example.test"
    )));
}

#[test]
fn nxdomain_for_unknown_name() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("no.such.name.test", TYPE_A, 4));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), dns_compress::RCODE_NXDOMAIN);
    assert!(msg.answers.is_empty());
}

#[test]
fn unsupported_qtype_gets_empty_noerror() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("localhost.test", 15, 5)); // MX
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), 0);
    assert!(msg.answers.is_empty());
}

#[test]
fn malformed_payload_gets_formerr_and_connection_lives() {
    let port = spawn_server();
    let mut s = connect(port);
    // 12 字节头部 + QDCOUNT=1，但名字畸形（标签越界）。
    let mut bad = Vec::new();
    bad.extend_from_slice(&0x9999u16.to_be_bytes());
    bad.extend_from_slice(&0x0100u16.to_be_bytes());
    bad.extend_from_slice(&1u16.to_be_bytes());
    bad.extend_from_slice(&0u16.to_be_bytes());
    bad.extend_from_slice(&0u16.to_be_bytes());
    bad.extend_from_slice(&0u16.to_be_bytes());
    bad.push(60);
    bad.extend_from_slice(b"ab"); // 声称 60 字节实际 2

    let resp = round(&mut s, &bad);
    let msg = Message::parse(&resp).unwrap();
    assert_eq!(msg.header.id, 0x9999);
    assert_eq!(msg.header.qr(), 1);
    assert_eq!(msg.header.rcode(), dns_compress::RCODE_FORMERR);

    // 同一连接上后续正常请求仍能被处理。
    let ok = round(&mut s, &query("localhost.test", TYPE_A, 6));
    let msg2 = Message::parse(&ok).unwrap();
    assert_eq!(msg2.header.rcode(), 0);
}

#[test]
fn case_insensitive_name_lookup() {
    let port = spawn_server();
    let mut s = connect(port);
    let resp_bytes = round(&mut s, &query("LOCALHOST.Test", TYPE_A, 7));
    let msg = Message::parse(&resp_bytes).unwrap();
    assert_eq!(msg.header.rcode(), 0);
    assert!(msg
        .answers
        .iter()
        .any(|r| matches!(r.rdata, dns_compress::Rdata::A([127, 0, 0, 1]))));
}
