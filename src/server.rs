//! 本地 TCP 测试服务。
//!
//! 协议：RFC 1035 §4.2.2 的 TCP 分帧——每条消息前带 2 字节大端长度前缀。
//! 服务内置一张静态“区”，只实现有限子集应答：
//! - QTYPE 为 A/AAAA/CNAME 且 QCLASS=IN 时做名字查询；
//! - 未知 QTYPE / 非 IN 类：NOERROR 空答案（有限子集不实现）；
//! - 解析失败但能读到 ID：FORMERR；连头部都不完整：关闭连接。
//!
//! 编码复用库的“有限响应编码”（输出合法压缩指针）。

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::Duration;

use crate::message::{
    Header, Message, Rdata, Record, CLASS_IN, MAX_MESSAGE_LEN, RCODE_FORMERR, RCODE_NOERROR,
    RCODE_NXDOMAIN, TYPE_A, TYPE_AAAA, TYPE_CNAME,
};
use crate::name::Name;
use crate::DnsError;

/// 静态区中一条名字对应的记录集合。
#[derive(Debug, Clone)]
pub struct ZoneEntry {
    pub a: Vec<[u8; 4]>,
    pub aaaa: Vec<[u8; 16]>,
    /// 若设置，该名字是某目标名的别名（CNAME）。
    pub cname: Option<Name>,
}

impl ZoneEntry {
    fn new() -> Self {
        ZoneEntry {
            a: Vec::new(),
            aaaa: Vec::new(),
            cname: None,
        }
    }
}

/// 静态区：名字（小写线格式字节）-> 记录。
#[derive(Debug, Clone)]
pub struct Zone {
    entries: BTreeMap<Vec<u8>, ZoneEntry>,
    pub ttl: u32,
}

impl Zone {
    /// 构造内置测试区。
    pub fn demo() -> Self {
        let mut z = Zone {
            entries: BTreeMap::new(),
            ttl: 300,
        };
        let n = |s: &str| Name::from_text(s).expect("demo 区名字常量合法");

        let mut localhost = ZoneEntry::new();
        localhost.a.push([127, 0, 0, 1]);
        localhost.aaaa.push([
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1,
        ]);
        z.put(n("localhost.test"), localhost);

        let mut www = ZoneEntry::new();
        www.cname = Some(n("cdn.example.test"));
        z.put(n("www.example.test"), www);

        let mut cdn = ZoneEntry::new();
        cdn.a.push([192, 0, 2, 10]);
        cdn.aaaa.push(hex_aaaa("2001:0db8::000a").0);
        z.put(n("cdn.example.test"), cdn);

        let mut a4 = ZoneEntry::new();
        a4.a.push([192, 0, 2, 1]);
        z.put(n("a.example.test"), a4);

        let mut a6 = ZoneEntry::new();
        a6.aaaa.push(hex_aaaa("2001:db8::1").0);
        z.put(n("aaaa.example.test"), a6);

        z
    }

    fn put(&mut self, name: Name, e: ZoneEntry) {
        let key = lowercase_key(&name);
        self.entries.insert(key, e);
    }

    /// 查名字（大小写不敏感）。
    pub fn lookup(&self, name: &Name) -> Option<&ZoneEntry> {
        self.entries.get(&lowercase_key(name))
    }
}

fn lowercase_key(name: &Name) -> Vec<u8> {
    let mut k = Vec::new();
    for l in name.labels() {
        for b in l {
            k.push(b.to_ascii_lowercase());
        }
        k.push(b'.');
    }
    k
}

/// 极简 IPv6 文本解析（仅用于 demo 区常量），支持 `::` 压缩。
fn hex_aaaa(text: &str) -> ([u8; 16],) {
    let mut groups: [u16; 8] = [0; 8];
    let (left, right) = match text.split_once("::") {
        Some((l, r)) => (l, Some(r)),
        None => (text, None),
    };
    let left_parts: Vec<u16> = if left.is_empty() {
        vec![]
    } else {
        left.split(':').map(|s| u16::from_str_radix(s, 16).unwrap()).collect()
    };
    let right_parts: Vec<u16> = match right {
        None => text
            .split(':')
            .map(|s| u16::from_str_radix(s, 16).unwrap())
            .collect(),
        Some(r) => {
            if r.is_empty() {
                vec![]
            } else {
                r.split(':')
                    .map(|s| u16::from_str_radix(s, 16).unwrap())
                    .collect()
            }
        }
    };
    let gap = 8 - left_parts.len() - right_parts.len();
    for (i, v) in left_parts.iter().enumerate() {
        groups[i] = *v;
    }
    for (i, v) in right_parts.iter().enumerate() {
        groups[left_parts.len() + gap + i] = *v;
    }
    let mut out = [0u8; 16];
    for (i, g) in groups.iter().enumerate() {
        out[2 * i..2 * i + 2].copy_from_slice(&g.to_be_bytes());
    }
    (out,)
}

/// 依据解析后的请求与静态区构造应答（有限子集）。
pub fn answer(request: &Message, zone: &Zone) -> Message {
    let req = request.header;
    let question = request.questions.first();

    let rcode;
    let mut answers: Vec<Record> = Vec::new();

    match question {
        None => rcode = RCODE_FORMERR,
        Some(q) => {
            if q.qclass != CLASS_IN || ![TYPE_A, TYPE_AAAA, TYPE_CNAME].contains(&q.qtype) {
                // 有限子集：不支持的类/型返回 NOERROR + 空答案。
                rcode = RCODE_NOERROR;
            } else {
                // 若名字本身就是 CNAME 别名，CNAME 查询直接返回该 CNAME；
                // A/AAAA 查询则附 CNAME 链直到找到终端记录。
                let mut cur = q.name.clone();
                let mut steps = 0;
                loop {
                    match zone.lookup(&cur) {
                        None => {
                            rcode = if answers.is_empty() {
                                RCODE_NXDOMAIN
                            } else {
                                RCODE_NOERROR
                            };
                            break;
                        }
                        Some(entry) => {
                            if q.qtype == TYPE_CNAME {
                                if let Some(target) = &entry.cname {
                                    answers.push(cname_record(&cur, target, zone.ttl));
                                }
                                rcode = RCODE_NOERROR;
                                break;
                            }
                            // A / AAAA
                            if let Some(target) = &entry.cname {
                                answers.push(cname_record(&cur, target, zone.ttl));
                                cur = target.clone();
                                steps += 1;
                                if steps > 16 {
                                    // 防御别名环（demo 区不应出现）。
                                    rcode = RCODE_NOERROR;
                                    break;
                                }
                                continue;
                            }
                            if q.qtype == TYPE_A {
                                for ip in &entry.a {
                                    answers.push(Record {
                                        name: cur.clone(),
                                        rtype: TYPE_A,
                                        rclass: CLASS_IN,
                                        ttl: zone.ttl,
                                        rdata: Rdata::A(*ip),
                                    });
                                }
                            } else {
                                for ip in &entry.aaaa {
                                    answers.push(Record {
                                        name: cur.clone(),
                                        rtype: TYPE_AAAA,
                                        rclass: CLASS_IN,
                                        ttl: zone.ttl,
                                        rdata: Rdata::Aaaa(*ip),
                                    });
                                }
                            }
                            rcode = RCODE_NOERROR;
                            break;
                        }
                    }
                }
            }
        }
    }

    let flags = Header::build_flags(
        1,
        req.opcode(),
        0,
        0,
        req.rd(),
        1,
        0,
        rcode,
    );
    let questions = if question.is_some() {
        request.questions.clone()
    } else {
        Vec::new()
    };
    Message {
        header: Header {
            id: req.id,
            flags,
            qdcount: questions.len() as u16,
            ancount: answers.len() as u16,
            nscount: 0,
            arcount: 0,
        },
        questions,
        answers,
        authority: Vec::new(),
        additional: Vec::new(),
    }
}

fn cname_record(alias: &Name, target: &Name, ttl: u32) -> Record {
    Record {
        name: alias.clone(),
        rtype: TYPE_CNAME,
        rclass: CLASS_IN,
        ttl,
        rdata: Rdata::Cname(target.clone()),
    }
}

/// 处理一条已完成分帧的 DNS 请求字节，返回应答字节；
/// 解析失败时尽量回填请求 ID 返回 FORMERR。
pub fn handle_payload(payload: &[u8], zone: &Zone) -> std::result::Result<Vec<u8>, DnsError> {
    match Message::parse(payload) {
        Ok(msg) => {
            let resp = answer(&msg, zone);
            resp.encode()
        }
        Err(e) => {
            // 尝试从前 2 字节恢复 ID，构造最小 FORMERR 应答。
            let id = payload
                .get(0..2)
                .map(|b| u16::from_be_bytes([b[0], b[1]]))
                .ok_or(e)?;
            let resp = Message {
                header: Header {
                    id,
                    flags: Header::build_flags(1, 0, 0, 0, 0, 0, 0, RCODE_FORMERR),
                    qdcount: 0,
                    ancount: 0,
                    nscount: 0,
                    arcount: 0,
                },
                questions: Vec::new(),
                answers: Vec::new(),
                authority: Vec::new(),
                additional: Vec::new(),
            };
            resp.encode()
        }
    }
}

/// 在一条双向流上循环处理“长度前缀 + DNS 消息”，直到对端关闭。
/// 供 TCP 服务与集成测试（内存流/真 TcpStream）共用。
pub fn serve_connection<R: Read, W: Write>(
    mut reader: R,
    mut writer: W,
    zone: &Zone,
) -> std::io::Result<()> {
    let mut len_buf = [0u8; 2];
    loop {
        match reader.read_exact(&mut len_buf) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(e) => return Err(e),
        }
        let len = u16::from_be_bytes(len_buf) as usize;
        if len > MAX_MESSAGE_LEN {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                DnsError::MessageTooLong {
                    len,
                    max: MAX_MESSAGE_LEN,
                }
                .to_string(),
            ));
        }
        let mut payload = vec![0u8; len];
        reader.read_exact(&mut payload)?;

        let response = handle_payload(&payload, zone).unwrap_or_else(|_| Vec::new());
        if response.is_empty() {
            // 连 ID 都无法恢复：无法给出有意义应答，关闭连接。
            return Ok(());
        }
        writer.write_all(&(response.len() as u16).to_be_bytes())?;
        writer.write_all(&response)?;
        writer.flush()?;
    }
}

/// 阻塞式 TCP 监听主循环（每个连接一个线程）。
pub fn run(addr: &str) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr)?;
    let zone = Zone::demo();
    eprintln!("dns-compress TCP 测试服务监听于 {addr}");
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let z = zone.clone();
                std::thread::spawn(move || {
                    let _ = s.set_nodelay(true);
                    let _ = s.set_read_timeout(Some(Duration::from_secs(10)));
                    let peer = s
                        .peer_addr()
                        .map(|p| p.to_string())
                        .unwrap_or_else(|_| "?".into());
                    let (r, w) = (&s, &s);
                    if let Err(e) = serve_connection(r, w, &z) {
                        eprintln!("连接 {peer} 结束（错误：{e}）");
                    }
                });
            }
            Err(e) => eprintln!("接受连接失败：{e}"),
        }
    }
    Ok(())
}
