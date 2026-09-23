//! DNS 报文：头部、问题、资源记录；解析与有限编码。
//!
//! 支持子集（明确约定）：
//!
//! - 类型：A(1)、AAAA(28)、CNAME(5) 解析为结构化 RDATA；
//!   **未知类型按 RDLENGTH 原样保留字节**（[`Rdata::Unknown`]），编码时原样写回。
//! - 编码不做压缩（合法但更长）；解码完整支持压缩指针。
//! - 不实现 EDNS、TSIG、区传送等扩展。

use crate::error::{DnsError, Section};
use crate::name::Name;
use crate::parser::{Limits, Reader};

/// 类型：A（IPv4 地址）。
pub const TYPE_A: u16 = 1;
/// 类型：CNAME（规范名）。
pub const TYPE_CNAME: u16 = 5;
/// 类型：AAAA（IPv6 地址）。
pub const TYPE_AAAA: u16 = 28;
/// 类：IN（互联网）。
pub const CLASS_IN: u16 = 1;

/// 头部标志位（16 位）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Flags {
    /// 0=查询，1=响应。
    pub qr: bool,
    /// 操作码（0=标准查询）。
    pub opcode: u8,
    /// 权威应答。
    pub aa: bool,
    /// 截断。
    pub tc: bool,
    /// 期望递归。
    pub rd: bool,
    /// 递归可用。
    pub ra: bool,
    /// 保留位 Z（按位保留，回显时原样写回）。
    pub z: u8,
    /// 响应码（0=NOERROR, 1=FORMERR, 4=NOTIMP …）。
    pub rcode: u8,
}

impl Flags {
    fn from_u16(v: u16) -> Flags {
        Flags {
            qr: v & 0x8000 != 0,
            opcode: ((v >> 11) & 0xF) as u8,
            aa: v & 0x0400 != 0,
            tc: v & 0x0200 != 0,
            rd: v & 0x0100 != 0,
            ra: v & 0x0080 != 0,
            z: ((v >> 4) & 0x7) as u8,
            rcode: (v & 0xF) as u8,
        }
    }

    fn to_u16(self) -> u16 {
        (if self.qr { 0x8000 } else { 0 })
            | (((self.opcode & 0xF) as u16) << 11)
            | (if self.aa { 0x0400 } else { 0 })
            | (if self.tc { 0x0200 } else { 0 })
            | (if self.rd { 0x0100 } else { 0 })
            | (if self.ra { 0x0080 } else { 0 })
            | (((self.z & 0x7) as u16) << 4)
            | ((self.rcode & 0xF) as u16)
    }
}

/// 一个问题。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Question {
    /// 查询名。
    pub qname: Name,
    /// 查询类型。
    pub qtype: u16,
    /// 查询类。
    pub qclass: u16,
}

/// 资源记录数据。未知类型原样保留字节。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Rdata {
    /// A：IPv4 地址。
    A(std::net::Ipv4Addr),
    /// AAAA：IPv6 地址。
    Aaaa(std::net::Ipv6Addr),
    /// CNAME：规范名（解码时支持压缩指针）。
    Cname(Name),
    /// 未识别类型：`(type, 原始字节)`。
    Unknown(u16, Vec<u8>),
}

impl Rdata {
    /// 该 RDATA 对应的类型码。
    pub fn rtype(&self) -> u16 {
        match self {
            Rdata::A(_) => TYPE_A,
            Rdata::Aaaa(_) => TYPE_AAAA,
            Rdata::Cname(_) => TYPE_CNAME,
            Rdata::Unknown(t, _) => *t,
        }
    }
}

impl std::fmt::Display for Rdata {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Rdata::A(a) => write!(f, "{a}"),
            Rdata::Aaaa(a) => write!(f, "{a}"),
            Rdata::Cname(n) => write!(f, "{n}"),
            Rdata::Unknown(_, bytes) => {
                write!(f, "\\# {} ", bytes.len())?;
                for b in bytes {
                    write!(f, "{b:02x}")?;
                }
                Ok(())
            }
        }
    }
}

/// 一条资源记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceRecord {
    /// 属主名。
    pub name: Name,
    /// 类型码（与 `rdata` 的类型一致）。
    pub rtype: u16,
    /// 类。
    pub rclass: u16,
    /// 生存时间（秒）。
    pub ttl: u32,
    /// 数据。
    pub rdata: Rdata,
}

/// 一条完整 DNS 报文。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    /// 事务 ID。
    pub id: u16,
    /// 标志位。
    pub flags: Flags,
    /// 问题段。
    pub questions: Vec<Question>,
    /// 回答段。
    pub answers: Vec<ResourceRecord>,
    /// 授权段。
    pub authorities: Vec<ResourceRecord>,
    /// 附加段。
    pub additionals: Vec<ResourceRecord>,
}

impl Message {
    /// 解析整份报文（TCP 帧去掉两字节前缀后的内容，或 UDP 载荷）。
    pub fn parse(msg: &[u8], limits: &Limits) -> Result<Message, DnsError> {
        if msg.len() < 12 {
            return Err(DnsError::MessageTooShort);
        }
        let mut r = Reader::new(msg);
        let id = r.u16()?;
        let flags = Flags::from_u16(r.u16()?);
        let qd = r.u16()? as usize;
        let an = r.u16()? as usize;
        let ns = r.u16()? as usize;
        let ar = r.u16()? as usize;

        let mut questions = Vec::with_capacity(qd.min(64));
        for _ in 0..qd {
            if questions.len() >= limits.max_records_per_section {
                return Err(DnsError::TooManyRecords(Section::Question));
            }
            let (qname, _) = Name::parse(&mut r, limits)?;
            let qtype = r.u16()?;
            let qclass = r.u16()?;
            questions.push(Question {
                qname,
                qtype,
                qclass,
            });
        }

        let answers = parse_rr_section(&mut r, limits, an, Section::Answer)?;
        let authorities = parse_rr_section(&mut r, limits, ns, Section::Authority)?;
        let additionals = parse_rr_section(&mut r, limits, ar, Section::Additional)?;

        Ok(Message {
            id,
            flags,
            questions,
            answers,
            authorities,
            additionals,
        })
    }

    /// 编码为线格式（不使用压缩；未知类型 RDATA 原样写回）。
    pub fn encode(&self, limits: &Limits) -> Result<Vec<u8>, DnsError> {
        let mut out = Vec::with_capacity(64);
        out.extend_from_slice(&self.id.to_be_bytes());
        out.extend_from_slice(&self.flags.to_u16().to_be_bytes());
        push_count(&mut out, self.questions.len(), Section::Question, limits)?;
        push_count(&mut out, self.answers.len(), Section::Answer, limits)?;
        push_count(&mut out, self.authorities.len(), Section::Authority, limits)?;
        push_count(
            &mut out,
            self.additionals.len(),
            Section::Additional,
            limits,
        )?;

        for q in &self.questions {
            q.qname.encode_into(&mut out);
            out.extend_from_slice(&q.qtype.to_be_bytes());
            out.extend_from_slice(&q.qclass.to_be_bytes());
        }
        for rr in self
            .answers
            .iter()
            .chain(&self.authorities)
            .chain(&self.additionals)
        {
            rr.name.encode_into(&mut out);
            out.extend_from_slice(&rr.rtype.to_be_bytes());
            out.extend_from_slice(&rr.rclass.to_be_bytes());
            out.extend_from_slice(&rr.ttl.to_be_bytes());
            let rdlen_pos = out.len();
            out.extend_from_slice(&[0, 0]); // RDLENGTH 占位
            let rdata_start = out.len();
            match &rr.rdata {
                Rdata::A(a) => out.extend_from_slice(&a.octets()),
                Rdata::Aaaa(a) => out.extend_from_slice(&a.octets()),
                Rdata::Cname(n) => n.encode_into(&mut out),
                Rdata::Unknown(_, bytes) => out.extend_from_slice(bytes),
            }
            let rdlen = out.len() - rdata_start;
            if rdlen > u16::MAX as usize {
                return Err(DnsError::MessageTooLong);
            }
            out[rdlen_pos] = (rdlen >> 8) as u8;
            out[rdlen_pos + 1] = (rdlen & 0xFF) as u8;
        }

        if out.len() > u16::MAX as usize {
            return Err(DnsError::MessageTooLong);
        }
        Ok(out)
    }
}

fn push_count(
    out: &mut Vec<u8>,
    n: usize,
    section: Section,
    limits: &Limits,
) -> Result<(), DnsError> {
    if n > limits.max_records_per_section {
        return Err(DnsError::TooManyRecords(section));
    }
    out.extend_from_slice(&(n as u16).to_be_bytes());
    Ok(())
}

fn parse_rr_section(
    r: &mut Reader<'_>,
    limits: &Limits,
    count: usize,
    section: Section,
) -> Result<Vec<ResourceRecord>, DnsError> {
    let mut out = Vec::with_capacity(count.min(64));
    for _ in 0..count {
        if out.len() >= limits.max_records_per_section {
            return Err(DnsError::TooManyRecords(section));
        }
        out.push(parse_rr(r, limits)?);
    }
    Ok(out)
}

fn parse_rr(r: &mut Reader<'_>, limits: &Limits) -> Result<ResourceRecord, DnsError> {
    let (name, _) = Name::parse(r, limits)?;
    let rtype = r.u16()?;
    let rclass = r.u16()?;
    let ttl = r.u32()?;
    let rdlen = r.u16()? as usize;
    let rdata_start = r.pos();
    // 截断的资源记录：RDLENGTH 越过报文末尾。
    if rdata_start + rdlen > r.len() {
        return Err(DnsError::RdataLengthMismatch {
            declared: rdlen,
            available: r.len().saturating_sub(rdata_start),
        });
    }
    let rdata = parse_rdata(r, limits, rtype, rdlen)?;
    // 无论 RDATA 内部如何解析，外层游标严格按 RDLENGTH 推进。
    r.skip_to(rdata_start + rdlen)?;
    Ok(ResourceRecord {
        name,
        rtype,
        rclass,
        ttl,
        rdata,
    })
}

fn parse_rdata(
    r: &mut Reader<'_>,
    limits: &Limits,
    rtype: u16,
    rdlen: usize,
) -> Result<Rdata, DnsError> {
    match rtype {
        TYPE_A => {
            if rdlen != 4 {
                return Err(DnsError::InvalidRdataLen {
                    rtype: TYPE_A,
                    declared: rdlen,
                    expected: 4,
                });
            }
            let b = r.take(4)?;
            Ok(Rdata::A(std::net::Ipv4Addr::new(b[0], b[1], b[2], b[3])))
        }
        TYPE_AAAA => {
            if rdlen != 16 {
                return Err(DnsError::InvalidRdataLen {
                    rtype: TYPE_AAAA,
                    declared: rdlen,
                    expected: 16,
                });
            }
            let b = r.take(16)?;
            let mut o = [0u8; 16];
            o.copy_from_slice(b);
            Ok(Rdata::Aaaa(std::net::Ipv6Addr::from(o)))
        }
        TYPE_CNAME => {
            // RDATA 中的名字：从 RDATA 起始绝对偏移解析，指针相对整份报文。
            // 物理消耗必须恰好等于 RDLENGTH：名字越过 RDLENGTH 或 RDATA 有
            // 多余尾巴都属于畸形记录（指针目标本身仍可指向报文任意位置）。
            let start = r.pos();
            let mut sub = Reader::at_offset(r.msg_ref(), start)?;
            let (name, consumed) = Name::parse(&mut sub, limits)
                .map_err(|e| DnsError::InvalidRdataName(Box::new(e)))?;
            if consumed != rdlen {
                return Err(DnsError::RdataLengthMismatch {
                    declared: rdlen,
                    available: consumed,
                });
            }
            Ok(Rdata::Cname(name))
        }
        other => {
            // 未知类型：原样保留字节。
            let bytes = r.take(rdlen)?.to_vec();
            Ok(Rdata::Unknown(other, bytes))
        }
    }
}

impl std::fmt::Display for Message {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        writeln!(
            f,
            ";; id={} qr={} opcode={} aa={} tc={} rd={} ra={} z={} rcode={}",
            self.id,
            self.flags.qr as u8,
            self.flags.opcode,
            self.flags.aa as u8,
            self.flags.tc as u8,
            self.flags.rd as u8,
            self.flags.ra as u8,
            self.flags.z,
            self.flags.rcode
        )?;
        writeln!(
            f,
            ";; qd={} an={} ns={} ar={}",
            self.questions.len(),
            self.answers.len(),
            self.authorities.len(),
            self.additionals.len()
        )?;
        for q in &self.questions {
            writeln!(
                f,
                ";{} {} {}",
                q.qname,
                class_str(q.qclass),
                type_str(q.qtype)
            )?;
        }
        let sections = [
            ("ANSWER", &self.answers),
            ("AUTHORITY", &self.authorities),
            ("ADDITIONAL", &self.additionals),
        ];
        for (title, rrs) in sections {
            if rrs.is_empty() {
                continue;
            }
            writeln!(f, ";; {title} SECTION:")?;
            for rr in rrs {
                writeln!(
                    f,
                    "{} {} {} {} {}",
                    rr.name,
                    rr.ttl,
                    class_str(rr.rclass),
                    type_str(rr.rtype),
                    rr.rdata
                )?;
            }
        }
        Ok(())
    }
}

fn type_str(t: u16) -> String {
    match t {
        TYPE_A => "A".into(),
        TYPE_CNAME => "CNAME".into(),
        TYPE_AAAA => "AAAA".into(),
        other => format!("TYPE{other}"),
    }
}

fn class_str(c: u16) -> String {
    match c {
        CLASS_IN => "IN".into(),
        other => format!("CLASS{other}"),
    }
}
