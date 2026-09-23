//! 本地 TCP 测试服务的应答构造逻辑（与网络 I/O 解耦，便于测试）。
//!
//! 服务规则：
//!
//! - 收到无法解析的报文：回 FORMERR（rcode=1），ID 尽量从原始字节前 2 字节回显。
//! - 收到 QR=1 的报文：视为“回显测试”，解析后重新编码原样返回（用于验证
//!   解码→编码语义往返，含压缩输入）。
//! - 收到 QR=0 的查询：
//!   - 无问题：NOERROR 空应答；
//!   - 第一个问题类型为 A/AAAA/CNAME：回一条固定测试记录（TTL 60）；
//!   - 其他类型：NOTIMP（rcode=4）。

use std::net::{Ipv4Addr, Ipv6Addr};

use crate::error::DnsError;
use crate::message::{
    Flags, Message, Question, Rdata, ResourceRecord, CLASS_IN, TYPE_A, TYPE_AAAA, TYPE_CNAME,
};
use crate::name::Name;
use crate::parser::Limits;

/// 测试应答使用的固定 TTL。
pub const TEST_TTL: u32 = 60;

/// 对一帧请求字节构造响应字节。
pub fn respond(request: &[u8], limits: &Limits) -> Vec<u8> {
    match Message::parse(request, limits) {
        Ok(msg) => build_response(&msg, limits),
        Err(_) => formerr_response(request),
    }
}

/// 解析失败时的 FORMERR 响应：能取到 ID 就回显，否则 ID=0。
fn formerr_response(request: &[u8]) -> Vec<u8> {
    let id = if request.len() >= 2 {
        u16::from_be_bytes([request[0], request[1]])
    } else {
        0
    };
    let mut out = Vec::with_capacity(12);
    out.extend_from_slice(&id.to_be_bytes());
    // QR=1, RCODE=1(FORMERR)
    out.extend_from_slice(&0x8001u16.to_be_bytes());
    out.extend_from_slice(&[0; 8]);
    out
}

fn build_response(query: &Message, limits: &Limits) -> Vec<u8> {
    if query.flags.qr {
        // 回显模式：解析后重新编码（压缩输入 → 非压缩输出）。
        return query
            .encode(limits)
            .unwrap_or_else(|_| formerr_response(&query.id.to_be_bytes()));
    }

    let mut resp = Message {
        id: query.id,
        flags: Flags {
            qr: true,
            opcode: query.flags.opcode,
            rd: query.flags.rd,
            ra: true,
            ..Flags::default()
        },
        questions: query.questions.clone(),
        answers: Vec::new(),
        authorities: Vec::new(),
        additionals: Vec::new(),
    };

    let Some(q) = query.questions.first() else {
        return resp.encode(limits).unwrap_or_default();
    };

    let answer = match q.qtype {
        TYPE_A => Some(Rdata::A(Ipv4Addr::new(192, 0, 2, 1))),
        TYPE_AAAA => Some(Rdata::Aaaa(Ipv6Addr::new(0x2001, 0x0db8, 0, 0, 0, 0, 0, 1))),
        TYPE_CNAME => Some(Rdata::Cname(
            Name::from_labels(["alias".as_bytes(), "example".as_bytes(), "com".as_bytes()])
                .expect("固定名字必然合法"),
        )),
        _ => None,
    };

    match answer {
        Some(rdata) => {
            resp.answers.push(ResourceRecord {
                name: q.qname.clone(),
                rtype: q.qtype,
                rclass: CLASS_IN,
                ttl: TEST_TTL,
                rdata,
            });
        }
        None => {
            resp.flags.rcode = 4; // NOTIMP
        }
    }

    resp.encode(limits).unwrap_or_default()
}

/// 供二进制入口使用：逐连接处理，直到对端关闭或出错。
///
/// 调用方可在 `stream` 上设置读超时；超时作为 I/O 错误结束本连接处理。
pub fn serve_connection(stream: &mut std::net::TcpStream, limits: &Limits, max_msg: usize) {
    use crate::frame::{recv_frame, send_frame, FrameError};
    let peer = stream
        .peer_addr()
        .map(|a| a.to_string())
        .unwrap_or_else(|_| "?".into());
    loop {
        match recv_frame(stream, max_msg) {
            Ok(req) => {
                let resp = respond(&req, limits);
                if let Err(e) = send_frame(stream, &resp) {
                    eprintln!("[{peer}] 发送响应失败：{e}");
                    return;
                }
            }
            Err(FrameError::Closed) => return,
            // WouldBlock（读超时）也作为正常结束，保证测试/服务可退出。
            Err(FrameError::Io(ref e))
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                return
            }
            Err(e) => {
                eprintln!("[{peer}] 帧错误：{e}");
                return;
            }
        }
    }
}

/// 构造一个待解析的问题（供示例与测试复用）。
pub fn make_question(name: &str, qtype: u16) -> Result<Question, DnsError> {
    Ok(Question {
        qname: Name::from_dotted(name)?,
        qtype,
        qclass: CLASS_IN,
    })
}
