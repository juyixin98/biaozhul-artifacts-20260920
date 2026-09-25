//! DNS 报文读取（解析）与有限响应编码。
//!
//! 支持的“有限子集”：
//! - 问题段：任意 QTYPE/QCLASS（原值保留）。
//! - 资源记录 RDATA：仅结构化解析 **A（1）、AAAA（28）、CNAME（5）**；
//!   其他 TYPE 一律作为 [`Rdata::Unknown`] 保留**原始字节**，
//!   往返编码时逐字节还原。
//! - 报文长度上限 [`MAX_MESSAGE_LEN`]（65535）。

use crate::error::{DnsError, Result};
use crate::name::{write_name, Name, NameTable};
use crate::reader::Reader;

/// 报文最大长度（RFC 793 上 TCP 帧长字段为 16 位，最大 65535）。
pub const MAX_MESSAGE_LEN: usize = 65535;

// 已知类型常量。
pub const TYPE_A: u16 = 1;
pub const TYPE_CNAME: u16 = 5;
pub const TYPE_AAAA: u16 = 28;
pub const CLASS_IN: u16 = 1;

// 响应码。
pub const RCODE_NOERROR: u16 = 0;
pub const RCODE_FORMERR: u16 = 1;
pub const RCODE_SERVFAIL: u16 = 2;
pub const RCODE_NXDOMAIN: u16 = 3;
pub const RCODE_NOTIMP: u16 = 4;
pub const RCODE_REFUSED: u16 = 5;

/// 固定 12 字节的 DNS 头部。flags 整体保留原始 16 位，
/// 同时提供 QR/opcode/AA/TC/RD/RA/Z/RCODE 位操作便捷方法。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Header {
    pub id: u16,
    pub flags: u16,
    pub qdcount: u16,
    pub ancount: u16,
    pub nscount: u16,
    pub arcount: u16,
}

impl Header {
    pub fn qr(&self) -> u16 {
        (self.flags >> 15) & 1
    }
    pub fn opcode(&self) -> u16 {
        (self.flags >> 11) & 0xF
    }
    pub fn aa(&self) -> u16 {
        (self.flags >> 10) & 1
    }
    pub fn tc(&self) -> u16 {
        (self.flags >> 9) & 1
    }
    pub fn rd(&self) -> u16 {
        (self.flags >> 8) & 1
    }
    pub fn ra(&self) -> u16 {
        (self.flags >> 7) & 1
    }
    pub fn z(&self) -> u16 {
        (self.flags >> 4) & 0x7
    }
    pub fn rcode(&self) -> u16 {
        self.flags & 0xF
    }

    /// 组装 flags：调用方逐字段给出。
    #[allow(clippy::too_many_arguments)]
    pub fn build_flags(
        qr: u16,
        opcode: u16,
        aa: u16,
        tc: u16,
        rd: u16,
        ra: u16,
        z: u16,
        rcode: u16,
    ) -> u16 {
        ((qr & 1) << 15)
            | ((opcode & 0xF) << 11)
            | ((aa & 1) << 10)
            | ((tc & 1) << 9)
            | ((rd & 1) << 8)
            | ((ra & 1) << 7)
            | ((z & 0x7) << 4)
            | (rcode & 0xF)
    }
}

/// 问题段条目。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Question {
    pub name: Name,
    pub qtype: u16,
    pub qclass: u16,
}

/// 资源记录的 RDATA（有限子集）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Rdata {
    /// IPv4 地址，4 字节。
    A([u8; 4]),
    /// IPv6 地址，16 字节。
    Aaaa([u8; 16]),
    /// 规范名（解压后的域名）。
    Cname(Name),
    /// 所有未识别类型：原始 RDATA 字节原样保留。
    Unknown { rtype: u16, raw: Vec<u8> },
}

/// 一条资源记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub name: Name,
    pub rtype: u16,
    pub rclass: u16,
    pub ttl: u32,
    pub rdata: Rdata,
}

/// 完整 DNS 报文。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Message {
    pub header: Header,
    pub questions: Vec<Question>,
    pub answers: Vec<Record>,
    pub authority: Vec<Record>,
    pub additional: Vec<Record>,
}

impl Message {
    /// 从完整报文字节增量解析。任何越界/计数不符都返回明确错误。
    pub fn parse(bytes: &[u8]) -> Result<Message> {
        if bytes.len() > MAX_MESSAGE_LEN {
            return Err(DnsError::MessageTooLong {
                len: bytes.len(),
                max: MAX_MESSAGE_LEN,
            });
        }
        let mut r = Reader::new(bytes);
        let header = parse_header(&mut r)?;

        let mut questions = Vec::with_capacity(header.qdcount as usize);
        for _ in 0..header.qdcount {
            questions.push(parse_question(&mut r)?);
        }
        let mut answers = Vec::with_capacity(header.ancount as usize);
        for _ in 0..header.ancount {
            answers.push(parse_record(&mut r)?);
        }
        let mut authority = Vec::with_capacity(header.nscount as usize);
        for _ in 0..header.nscount {
            authority.push(parse_record(&mut r)?);
        }
        let mut additional = Vec::with_capacity(header.arcount as usize);
        for _ in 0..header.arcount {
            additional.push(parse_record(&mut r)?);
        }
        if !r.is_empty() {
            return Err(DnsError::Framing(format!(
                "报文在全部记录之后仍有 {} 字节多余数据",
                r.remaining()
            )));
        }
        Ok(Message {
            header,
            questions,
            answers,
            authority,
            additional,
        })
    }

    /// 有限编码：输出含压缩指针的标准线格式。
    /// 头部四个计数字段按各 section 实际长度重算。
    pub fn encode(&self) -> Result<Vec<u8>> {
        let mut out = Vec::with_capacity(64);
        let mut table = NameTable::new();

        // 头部占位，最后回填计数。
        out.extend_from_slice(&self.header.id.to_be_bytes());
        out.extend_from_slice(&self.header.flags.to_be_bytes());
        out.extend_from_slice(&(self.questions.len() as u16).to_be_bytes());
        out.extend_from_slice(&(self.answers.len() as u16).to_be_bytes());
        out.extend_from_slice(&(self.authority.len() as u16).to_be_bytes());
        out.extend_from_slice(&(self.additional.len() as u16).to_be_bytes());

        for q in &self.questions {
            write_name(&mut out, &q.name, &mut table)?;
            out.extend_from_slice(&q.qtype.to_be_bytes());
            out.extend_from_slice(&q.qclass.to_be_bytes());
        }
        for rec in self
            .answers
            .iter()
            .chain(&self.authority)
            .chain(&self.additional)
        {
            encode_record(rec, &mut out, &mut table)?;
        }

        if out.len() > MAX_MESSAGE_LEN {
            return Err(DnsError::MessageTooLong {
                len: out.len(),
                max: MAX_MESSAGE_LEN,
            });
        }
        Ok(out)
    }
}

fn parse_header(r: &mut Reader<'_>) -> Result<Header> {
    // 头部固定 12 字节；任何一个字段缺失都视为 UnexpectedEof（截断）。
    let id = r.u16()?;
    let flags = r.u16()?;
    let qdcount = r.u16()?;
    let ancount = r.u16()?;
    let nscount = r.u16()?;
    let arcount = r.u16()?;
    Ok(Header {
        id,
        flags,
        qdcount,
        ancount,
        nscount,
        arcount,
    })
}

fn parse_question(r: &mut Reader<'_>) -> Result<Question> {
    let (name, _) = Name::parse(r)?;
    let qtype = r.u16()?;
    let qclass = r.u16()?;
    Ok(Question {
        name,
        qtype,
        qclass,
    })
}

fn parse_record(r: &mut Reader<'_>) -> Result<Record> {
    let (name, _) = Name::parse(r)?;
    let rtype = r.u16()?;
    let rclass = r.u16()?;
    let ttl = r.u32()?;
    let rdlen = r.u16()? as usize;

    // RDLENGTH 划定的范围若超出报文 -> 明确的“截断资源记录”。
    let rdata_start = r.position();
    let mut sub = r.sub_reader(rdata_start, rdlen)?;

    let rdata = match rtype {
        TYPE_A => {
            if rdlen != 4 {
                return Err(DnsError::TruncatedRecord {
                    at: rdata_start,
                    declared: rdlen,
                    actual: rdlen,
                });
            }
            let bytes = sub.take(4)?;
            let mut a = [0u8; 4];
            a.copy_from_slice(bytes);
            Rdata::A(a)
        }
        TYPE_AAAA => {
            if rdlen != 16 {
                return Err(DnsError::TruncatedRecord {
                    at: rdata_start,
                    declared: rdlen,
                    actual: rdlen,
                });
            }
            let bytes = sub.take(16)?;
            let mut a = [0u8; 16];
            a.copy_from_slice(bytes);
            Rdata::Aaaa(a)
        }
        TYPE_CNAME => {
            let (cn, _) = Name::parse(&mut sub)?;
            if !sub.is_empty() {
                return Err(DnsError::Framing(format!(
                    "CNAME RDATA 在名字之后多出 {} 字节",
                    sub.remaining()
                )));
            }
            Rdata::Cname(cn)
        }
        other => {
            // 未知类型：原字节保留。
            let raw = sub.take(rdlen)?.to_vec();
            Rdata::Unknown {
                rtype: other,
                raw,
            }
        }
    };

    // 跳过 RDLENGTH 字节（游标推进到记录末尾）。
    r.skip(rdlen)?;
    Ok(Record {
        name,
        rtype,
        rclass,
        ttl,
        rdata,
    })
}

fn encode_record(rec: &Record, out: &mut Vec<u8>, table: &mut NameTable) -> Result<()> {
    write_name(out, &rec.name, table)?;
    out.extend_from_slice(&rec.rtype.to_be_bytes());
    out.extend_from_slice(&rec.rclass.to_be_bytes());
    out.extend_from_slice(&rec.ttl.to_be_bytes());
    let len_pos = out.len();
    out.extend_from_slice(&0u16.to_be_bytes()); // RDLENGTH 占位
    let data_start = out.len();

    match &rec.rdata {
        Rdata::A(a) => out.extend_from_slice(a),
        Rdata::Aaaa(a) => out.extend_from_slice(a),
        Rdata::Cname(n) => write_name(out, n, table)?,
        Rdata::Unknown { raw, .. } => out.extend_from_slice(raw),
    }

    let rdlen = out.len() - data_start;
    if rdlen > u16::MAX as usize {
        return Err(DnsError::MessageTooLong {
            len: rdlen,
            max: u16::MAX as usize,
        });
    }
    out[len_pos..len_pos + 2].copy_from_slice(&(rdlen as u16).to_be_bytes());
    Ok(())
}
