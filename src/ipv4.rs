//! # IPv4 报文解析（只解析重组所需的最小集合）
//!
//! 刻意不使用 pnet / etherparse 等现成协议解析器；IPv4 头字段解析基于
//! [`crate::parser`] 的增量原语手写完成。只覆盖重组关心的字段：
//! version、IHL、总长度、Identification、Flags/Fragment Offset、Protocol、
//! 源/目的地址、头部校验和（可选验证）与负载切片。
//!
//! 支持以**增量**方式喂入：[`parse_ipv4_inc`] 在数据不足时返回
//! [`ParseError::Incomplete`]，调用方可继续 feed 后重试。

use crate::parser::{ByteStream, Ctx, Cursor, ParseError, ParseResult};
use std::net::Ipv4Addr;

/// IPv4 分片标志位中的 MF（More Fragments）掩码。
pub const IP_FLAG_MF: u16 = 0x2000;
/// DF（Don't Fragment）掩码。
pub const IP_FLAG_DF: u16 = 0x4000;
/// Flags+Fragment Offset 字段中保留位掩码（RFC 791 中必须为 0）。
pub const IP_FLAG_RESERVED: u16 = 0x8000;
/// 片偏移以 8 字节为单位。
pub const FRAG_OFFSET_UNIT: usize = 8;
/// 不含选项的最小 IPv4 头长度（字节）。
pub const MIN_IPV4_HEADER_LEN: usize = 20;

/// 解析后的 IPv4 报文视图：头部信息 + 指向原始缓冲的负载切片。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ipv4Packet<'a> {
    pub src: Ipv4Addr,
    pub dst: Ipv4Addr,
    /// 上层协议号（TCP=6, UDP=17 ……）。
    pub protocol: u8,
    pub identification: u16,
    /// 是否还有后续片（MF 位）。
    pub more_fragments: bool,
    /// 片偏移（**字节**单位，已把 8 字节单位换算好）。
    pub fragment_offset: usize,
    /// 本报文承载的负载切片。
    pub payload: &'a [u8],
    /// 完整 IPv4 数据报声明的总长度（头 + 负载）。
    pub total_length: usize,
    /// 实际头部长度（含选项）。
    pub header_length: usize,
}

impl<'a> Ipv4Packet<'a> {
    /// 是否为“首片”（偏移为 0）。
    pub fn is_first_fragment(&self) -> bool {
        self.fragment_offset == 0
    }

    /// 是否为“末片”（MF=0 且属于分片流量；MF=0 偏移 0 表示未分片）。
    pub fn is_last_fragment(&self) -> bool {
        !self.more_fragments
    }

    /// 是否为分片流量（偏移非 0，或 MF=1）。
    pub fn is_fragment(&self) -> bool {
        self.more_fragments || self.fragment_offset != 0
    }

    /// 负载在重组数据报中的字节区间 `[start, end)`。
    pub fn byte_range(&self) -> (usize, usize) {
        let start = self.fragment_offset;
        (start, start + self.payload.len())
    }
}

/// 解析参数（生成器/测试用）。
#[derive(Debug, Clone)]
pub struct HeaderParams {
    pub src: Ipv4Addr,
    pub dst: Ipv4Addr,
    pub protocol: u8,
    pub identification: u16,
    pub more_fragments: bool,
    /// 字节单位的片偏移，构造时必须是 8 的倍数。
    pub fragment_offset: usize,
    pub payload: Vec<u8>,
}

/// 由 [`HeaderParams`] 构造一个线格式 IPv4 报文（含正确头部校验和）。
/// 供测试与 `fraggen` 工具使用；不用于生产解析路径。
pub fn build_ipv4(p: &HeaderParams) -> Vec<u8> {
    assert!(
        p.fragment_offset.is_multiple_of(FRAG_OFFSET_UNIT) || !p.more_fragments,
        "non-final fragments must have an 8-byte aligned offset"
    );
    let total_length = MIN_IPV4_HEADER_LEN + p.payload.len();
    assert!(total_length <= u16::MAX as usize, "payload too large");

    let mut out = Vec::with_capacity(total_length);
    out.push(0x45); // version=4, ihl=5（无选项）
    out.push(0x00); // DSCP/ECN
    out.extend_from_slice(&(total_length as u16).to_be_bytes());
    out.extend_from_slice(&p.identification.to_be_bytes());

    let mut flags_frag: u16 = (p.fragment_offset / FRAG_OFFSET_UNIT) as u16;
    if p.more_fragments {
        flags_frag |= IP_FLAG_MF;
    }
    out.extend_from_slice(&flags_frag.to_be_bytes());

    out.push(64); // TTL
    out.push(p.protocol);
    out.extend_from_slice(&0u16.to_be_bytes()); // 校验和占位
    out.extend_from_slice(&p.src.octets());
    out.extend_from_slice(&p.dst.octets());
    out.extend_from_slice(&p.payload);

    let cksum = ones_complement_checksum(&out[..MIN_IPV4_HEADER_LEN]);
    out[10..12].copy_from_slice(&cksum.to_be_bytes());
    out
}

/// RFC 1071 反码和校验。传入字节切片（校验和字段应为 0）。
pub fn ones_complement_checksum(data: &[u8]) -> u16 {
    let mut sum: u32 = 0;
    let mut i = 0;
    while i + 1 < data.len() {
        sum += u16::from_be_bytes([data[i], data[i + 1]]) as u32;
        i += 2;
    }
    if i < data.len() {
        sum += (data[i] as u32) << 8;
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    !(sum as u16)
}

/// 验证报文 IPv4 头部校验和是否正确。
pub fn verify_header_checksum(header: &[u8]) -> bool {
    ones_complement_checksum(header) == 0
}

/// 一次性解析整块缓冲。
pub fn parse_ipv4(data: &[u8]) -> ParseResult<Ipv4Packet<'_>> {
    parse_ipv4_slice(data)
}

/// 增量解析：从 [`ByteStream`] 的可读窗口解析一个完整 IPv4 报文。
///
/// 当头部声明的总长度尚未全部到达时返回 [`ParseError::Incomplete`]，
/// 已消费字节应由调用方通过 [`ByteStream::consume_n`] 提交。
pub fn parse_ipv4_inc(stream: &ByteStream) -> ParseResult<Ipv4Packet<'_>> {
    parse_ipv4_slice(stream.readable())
}

/// 共享的解析实现：直接在字节切片上工作。
/// 返回值的生命周期绑定到输入切片，因此一次性入口与增量入口
///（切片来自 `ByteStream`）都可以安全借用原始缓冲。
fn parse_ipv4_slice(data: &[u8]) -> ParseResult<Ipv4Packet<'_>> {
    let mut cur = Cursor::new(data);

    let (version, ihl) = cur.nibbles().ctx("version/ihl")?;
    if version != 4 {
        return Err(ParseError::UnexpectedTag {
            field: "version",
            expected: 4,
            got: version as u64,
        });
    }
    let header_length = (ihl as usize) * 4;
    if header_length < MIN_IPV4_HEADER_LEN {
        return Err(ParseError::InvalidValue {
            field: "ihl",
            value: ihl as u64,
        });
    }

    let _tos = cur.u8()?;
    let total_length = cur.be_u16().ctx("total_length")? as usize;
    if total_length < header_length {
        return Err(ParseError::BadLength {
            field: "total_length",
            declared: total_length,
            remaining: header_length,
        });
    }
    let identification = cur.be_u16().ctx("identification")?;
    let flags_frag = cur.be_u16().ctx("flags_fragment_offset")?;
    if flags_frag & IP_FLAG_RESERVED != 0 {
        return Err(ParseError::InvalidValue {
            field: "flags.reserved",
            value: 1,
        });
    }
    let more_fragments = flags_frag & IP_FLAG_MF != 0;
    let fragment_offset = (flags_frag & 0x1fff) as usize * FRAG_OFFSET_UNIT;

    let _ttl = cur.u8()?;
    let protocol = cur.u8().ctx("protocol")?;
    let _header_checksum = cur.be_u16()?;
    let src_octets: [u8; 4] = cur.take(4)?.try_into().unwrap();
    let dst_octets: [u8; 4] = cur.take(4)?.try_into().unwrap();
    let src = Ipv4Addr::from(src_octets);
    let dst = Ipv4Addr::from(dst_octets);

    // 声明的总长度必须全部到达；尾部多余字节（如二层 padding）不属于报文。
    if data.len() < total_length {
        return Err(ParseError::Incomplete {
            need: total_length,
            have: data.len(),
        });
    }
    // 选项区 + 头部整体必须存在（最小头部前面已读，但 IHL 可能更大）。
    if data.len() < header_length {
        return Err(ParseError::Incomplete {
            need: header_length,
            have: data.len(),
        });
    }

    let header = &data[..header_length];
    if !verify_header_checksum(header) {
        return Err(ParseError::InvalidValue {
            field: "header_checksum",
            value: 0,
        });
    }

    let payload_len = total_length - header_length;
    let payload = &data[header_length..total_length];
    debug_assert_eq!(payload.len(), payload_len);

    Ok(Ipv4Packet {
        src,
        dst,
        protocol,
        identification,
        more_fragments,
        fragment_offset,
        payload,
        total_length,
        header_length,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(offset: usize, mf: bool, payload: &[u8], id: u16) -> Vec<u8> {
        build_ipv4(&HeaderParams {
            src: Ipv4Addr::new(10, 0, 0, 1),
            dst: Ipv4Addr::new(10, 0, 0, 2),
            protocol: 17,
            identification: id,
            more_fragments: mf,
            fragment_offset: offset,
            payload: payload.to_vec(),
        })
    }

    #[test]
    fn parses_basic_fragment_fields() {
        let raw = sample(0, true, &[1, 2, 3, 4, 5, 6, 7, 8], 0x1234);
        let pkt = parse_ipv4(&raw).unwrap();
        assert_eq!(pkt.identification, 0x1234);
        assert!(pkt.more_fragments);
        assert_eq!(pkt.fragment_offset, 0);
        assert_eq!(pkt.payload, &[1, 2, 3, 4, 5, 6, 7, 8]);
        assert_eq!(pkt.total_length, 28);
        assert!(pkt.is_first_fragment());
        assert!(pkt.is_fragment());

        let raw2 = sample(8, false, &[9, 10], 0x1234);
        let pkt2 = parse_ipv4(&raw2).unwrap();
        assert!(!pkt2.more_fragments);
        assert_eq!(pkt2.fragment_offset, 8);
        assert_eq!(pkt2.byte_range(), (8, 10));
        assert!(!pkt2.is_first_fragment());
    }

    #[test]
    fn incremental_parse_waits_for_whole_datagram() {
        let raw = sample(0, true, &[0xaa; 100], 7);
        let mut stream = ByteStream::new(2048);

        // 只给 10 字节：连最小头部都不全。
        stream.feed(&raw[..10]).unwrap();
        assert!(matches!(
            parse_ipv4_inc(&stream).unwrap_err(),
            ParseError::Incomplete { .. }
        ));

        // 给完整头部 + 部分负载：仍应 Incomplete（total_length 未到齐）。
        stream.feed(&raw[10..MIN_IPV4_HEADER_LEN + 30]).unwrap();
        assert!(matches!(
            parse_ipv4_inc(&stream).unwrap_err(),
            ParseError::Incomplete { .. }
        ));

        // 全部到齐后解析成功。
        stream.feed(&raw[MIN_IPV4_HEADER_LEN + 30..]).unwrap();
        let pkt = parse_ipv4_inc(&stream).unwrap();
        assert_eq!(pkt.payload.len(), 100);
    }

    #[test]
    fn rejects_bad_version_and_checksum() {
        let mut raw = sample(0, false, &[1], 1);
        raw[0] = 0x65; // version=6
        assert!(matches!(
            parse_ipv4(&raw).unwrap_err(),
            ParseError::UnexpectedTag { .. }
        ));

        let mut raw = sample(0, false, &[1], 1);
        raw[11] ^= 0xff; // 破坏协议字节，校验和不再匹配
        assert!(matches!(
            parse_ipv4(&raw).unwrap_err(),
            ParseError::InvalidValue { .. }
        ));
    }

    #[test]
    fn rejects_reserved_flag_and_short_total_length() {
        let mut raw = sample(0, false, &[1], 1);
        // 把保留位置 1
        let mut v = u16::from_be_bytes([raw[6], raw[7]]);
        v |= IP_FLAG_RESERVED;
        raw[6..8].copy_from_slice(&v.to_be_bytes());
        raw[10..12].copy_from_slice(&0u16.to_be_bytes());
        let cksum = ones_complement_checksum(&raw[..20]);
        raw[10..12].copy_from_slice(&cksum.to_be_bytes());
        assert!(parse_ipv4(&raw).is_err());

        let mut raw = sample(0, false, &[1], 1);
        raw[2..4].copy_from_slice(&10u16.to_be_bytes()); // total_length < header
        raw[10..12].copy_from_slice(&0u16.to_be_bytes());
        let cksum = ones_complement_checksum(&raw[..20]);
        raw[10..12].copy_from_slice(&cksum.to_be_bytes());
        assert!(matches!(
            parse_ipv4(&raw).unwrap_err(),
            ParseError::BadLength { .. }
        ));
    }
}
