//! # 手写 IPv4 头部解析与构造
//!
//! 本模块用 [`crate::byte_reader`] 手工解析 IPv4 报文（RFC 791），
//! **不使用任何现成协议解析库**。重组引擎和 TCP 服务都只消费这里的
//! 解析结果；[`build_fragment`] 等构造函数用于测试与请求样例。

use std::net::Ipv4Addr;

use crate::byte_reader::{ParseError, Reader};

/// IPv4 解析错误（显式分类，不 panic）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Ipv4Error {
    /// 报文被截断，字段读不完整；喂入完整报文后可恢复。
    Truncated {
        needed: Option<usize>,
        available: usize,
    },
    /// 结构合法但字段非法（版本号、IHL、总长度、标志位等）。
    Malformed { message: String },
}

impl Ipv4Error {
    fn malformed(msg: impl Into<String>) -> Self {
        Ipv4Error::Malformed {
            message: msg.into(),
        }
    }

    fn from_parse(e: ParseError) -> Self {
        match e {
            ParseError::Incomplete {
                needed,
                available,
            } => Ipv4Error::Truncated {
                needed,
                available,
            },
            ParseError::LimitExceeded {
                requested,
                remaining,
            } => Ipv4Error::Malformed {
                message: format!(
                    "options run past IHL-declared header (requested {requested}, {remaining} in header)"
                ),
            },
            ParseError::InvalidInput { message } => Ipv4Error::Malformed { message },
            ParseError::TrailingBytes { remaining } => Ipv4Error::Malformed {
                message: format!("{remaining} trailing byte(s) in declared header"),
            },
        }
    }
}

impl std::fmt::Display for Ipv4Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Ipv4Error::Truncated { needed, available } => match needed {
                Some(n) => write!(
                    f,
                    "truncated IPv4 packet: need {n} more byte(s), {available} available"
                ),
                None => write!(f, "truncated IPv4 packet: {available} byte(s) available"),
            },
            Ipv4Error::Malformed { message } => write!(f, "malformed IPv4 packet: {message}"),
        }
    }
}

impl std::error::Error for Ipv4Error {}

/// IPv4 头部（字段全部拷贝为所有权类型，生命周期不绑定原始报文）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ipv4Header {
    /// Internet Header Length（32 位字个数，最小值 5）
    pub ihl: u8,
    /// 头部长度（字节）= ihl * 4
    pub header_len: usize,
    pub dscp_ecn: u8,
    /// 头部 Total Length 字段
    pub total_length: u16,
    pub identification: u16,
    /// 保留标志位（bit 0）——合法报文必须为 false
    pub reserved_flag: bool,
    /// Don't Fragment
    pub df: bool,
    /// More Fragments
    pub mf: bool,
    /// 分片偏移（字节，已由 8 字节字换算）
    pub fragment_offset: usize,
    pub ttl: u8,
    pub protocol: u8,
    /// 头部校验和字段（网络序原值）
    pub checksum: u16,
    pub src: Ipv4Addr,
    pub dst: Ipv4Addr,
    /// 选项字节（无选项时为空）
    pub options: Vec<u8>,
}

impl Ipv4Header {
    /// 重组分组键的一部分：是否属于某个分片数据报。
    /// MF=1 或 offset>0 即为分片；MF=0 且 offset=0 是未分片完整数据报。
    pub fn is_fragment(&self) -> bool {
        self.mf || self.fragment_offset > 0
    }
}

/// 解析后的 IPv4 报文：头部 + 载荷零拷贝切片。
#[derive(Debug)]
pub struct Ipv4Packet<'a> {
    pub header: Ipv4Header,
    /// Total Length 界定的载荷切片
    pub payload: &'a [u8],
    /// 头部原始字节（可用于复验校验和）
    pub raw_header: &'a [u8],
    /// 头部校验和是否正确（解析时顺带计算）
    pub checksum_valid: bool,
}

/// 解析 IPv4 报文。
///
/// 校验严格性：
/// * 物理截断（连固定头部都读不完）→
///   [`Ipv4Error::Truncated`]；
/// * 版本号 ≠ 4、IHL < 5、Total Length < 头部长度、保留标志位置位、
///   DF 与非零偏移共存、非尾片载荷不是 8 字节倍数等 →
///   [`Ipv4Error::Malformed`]；
/// * 校验和**不**在这里作为硬拒绝条件（重组引擎另有策略），
///   结果见 [`Ipv4Packet::checksum_valid`]。
pub fn parse_ipv4(packet: &[u8]) -> Result<Ipv4Packet<'_>, Ipv4Error> {
    let mut r = Reader::new(packet);

    // 固定头部 20 字节，不足即截断
    let ver_ihl = r.read_u8().map_err(Ipv4Error::from_parse)?;
    let version = ver_ihl >> 4;
    let ihl = ver_ihl & 0x0f;
    if version != 4 {
        return Err(Ipv4Error::malformed(format!(
            "IP version is {version}, expected 4"
        )));
    }
    if ihl < 5 {
        return Err(Ipv4Error::malformed(format!(
            "IHL={ihl} (< 5 words, minimum 20 bytes)"
        )));
    }
    let header_len = (ihl as usize) * 4;
    if packet.len() < header_len {
        return Err(Ipv4Error::Truncated {
            needed: Some(header_len - packet.len()),
            available: packet.len(),
        });
    }

    let dscp_ecn = r.read_u8().map_err(Ipv4Error::from_parse)?;
    let total_length = r.read_u16_be().map_err(Ipv4Error::from_parse)? as usize;
    if total_length < header_len {
        return Err(Ipv4Error::malformed(format!(
            "total length {total_length} < header length {header_len}"
        )));
    }
    if packet.len() < total_length {
        return Err(Ipv4Error::Truncated {
            needed: Some(total_length - packet.len()),
            available: packet.len(),
        });
    }

    let identification = r.read_u16_be().map_err(Ipv4Error::from_parse)?;
    let flags_frag = r.read_u16_be().map_err(Ipv4Error::from_parse)?;
    let reserved_flag = flags_frag & 0x8000 != 0;
    if reserved_flag {
        return Err(Ipv4Error::malformed("reserved flag bit must be zero"));
    }
    let df = flags_frag & 0x4000 != 0;
    let mf = flags_frag & 0x2000 != 0;
    let fragment_offset = ((flags_frag & 0x1fff) as usize) * 8;
    if df && fragment_offset > 0 {
        return Err(Ipv4Error::malformed(
            "DF set together with nonzero fragment offset",
        ));
    }

    let ttl = r.read_u8().map_err(Ipv4Error::from_parse)?;
    let protocol = r.read_u8().map_err(Ipv4Error::from_parse)?;
    let checksum = r.read_u16_be().map_err(Ipv4Error::from_parse)?;

    let src = Ipv4Addr::from(r.read_u32_be().map_err(Ipv4Error::from_parse)?);
    let dst = Ipv4Addr::from(r.read_u32_be().map_err(Ipv4Error::from_parse)?);

    // 选项：用“子集”能力在 IHL 声明的头部边界内解析，越界即错误。
    let options_len = header_len - 20;
    let mut options_sub = r.take_subset(options_len).map_err(Ipv4Error::from_parse)?;
    let options = options_sub
        .read_rest()
        .map_err(Ipv4Error::from_parse)?
        .to_vec();
    options_sub.finish().map_err(Ipv4Error::malformed_from)?;

    let raw_header = &packet[..header_len];
    let payload = &packet[header_len..total_length];

    // 非尾片（MF=1）的载荷长度必须是 8 字节倍数，否则偏移无法表示
    if mf && !payload.len().is_multiple_of(8) {
        return Err(Ipv4Error::malformed(format!(
            "non-final fragment payload length {} is not a multiple of 8",
            payload.len()
        )));
    }

    let checksum_valid = internet_checksum(raw_header) == 0;

    Ok(Ipv4Packet {
        header: Ipv4Header {
            ihl,
            header_len,
            dscp_ecn,
            total_length: total_length as u16,
            identification,
            reserved_flag,
            df,
            mf,
            fragment_offset,
            ttl,
            protocol,
            checksum,
            src,
            dst,
            options,
        },
        payload,
        raw_header,
        checksum_valid,
    })
}

impl Ipv4Error {
    fn malformed_from(e: ParseError) -> Self {
        Ipv4Error::Malformed {
            message: e.to_string(),
        }
    }
}

/// 计算 RFC 1071 反码和（输入按 16 位字求和；奇数字节补 0）。
/// 对完整头部（含校验和字段）计算时，正确结果应为 0。
pub fn internet_checksum(bytes: &[u8]) -> u16 {
    let mut sum: u32 = 0;
    let (chunks, remainder) = bytes.as_chunks::<2>();
    for pair in chunks {
        sum += u16::from_be_bytes(*pair) as u32;
    }
    if let Some(&odd) = remainder.first() {
        sum += (odd as u32) << 8;
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    !(sum as u16)
}

/// 构造参数（[`build_fragment`] 用）：4 个八位组表示一个 IPv4 地址。
pub type Octets = (u8, u8, u8, u8);

/// 手工构造一个 IPv4 报文/分片（20 字节无选项头部，校验和自动计算）。
///
/// 当 `offset_bytes == 0 && !more_fragments` 时即为未分片完整数据报。
///
/// # 参数
/// * `src` / `dst`：四元组地址；
/// * `protocol`：上层协议号（如 17 = UDP）；
/// * `identification`：数据报 ID；
/// * `offset_bytes`：本分片数据相对原始数据报起点的字节偏移
///   （必须为 8 的倍数）；
/// * `more_fragments`：MF 标志；
/// * `payload`：本分片携带的数据。
pub fn build_fragment(
    src: Octets,
    dst: Octets,
    protocol: u8,
    identification: u16,
    offset_bytes: usize,
    more_fragments: bool,
    payload: Vec<u8>,
) -> Vec<u8> {
    build_packet_full(
        src,
        dst,
        protocol,
        identification,
        false,
        more_fragments,
        offset_bytes,
        64,
        0,
        &[],
        payload,
    )
}

/// 带完整可选项的构造器（TTL/服务类型/选项/DF），用于特殊测试。
#[allow(clippy::too_many_arguments)]
pub fn build_packet_full(
    src: Octets,
    dst: Octets,
    protocol: u8,
    identification: u16,
    df: bool,
    mf: bool,
    offset_bytes: usize,
    ttl: u8,
    dscp_ecn: u8,
    options: &[u8],
    payload: Vec<u8>,
) -> Vec<u8> {
    assert!(
        offset_bytes.is_multiple_of(8),
        "fragment offset must be a multiple of 8"
    );
    assert!(
        options.len().is_multiple_of(4),
        "options must pad to 32-bit boundary"
    );
    assert!(options.len() <= 40, "at most 40 option bytes");

    let ihl_words = 5 + options.len() / 4;
    let header_len = ihl_words * 4;
    let total_length = header_len + payload.len();
    assert!(
        total_length <= u16::MAX as usize,
        "total length exceeds 65535"
    );

    let mut packet = Vec::with_capacity(total_length);
    packet.push(0x40 | (ihl_words as u8));
    packet.push(dscp_ecn);
    packet.extend_from_slice(&(total_length as u16).to_be_bytes());
    packet.extend_from_slice(&identification.to_be_bytes());

    assert!(
        offset_bytes / 8 <= 0x1fff,
        "fragment offset exceeds 13 bits"
    );
    let mut flags_frag = ((offset_bytes / 8) as u16) & 0x1fff;
    if df {
        flags_frag |= 0x4000;
    }
    if mf {
        flags_frag |= 0x2000;
    }
    packet.extend_from_slice(&flags_frag.to_be_bytes());
    packet.push(ttl);
    packet.push(protocol);
    packet.extend_from_slice(&0u16.to_be_bytes()); // 校验和占位
    packet.extend_from_slice(&[src.0, src.1, src.2, src.3]);
    packet.extend_from_slice(&[dst.0, dst.1, dst.2, dst.3]);
    packet.extend_from_slice(options);
    packet.extend_from_slice(&payload);

    let cksum = internet_checksum(&packet[..header_len]);
    packet[10..12].copy_from_slice(&cksum.to_be_bytes());
    packet
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_roundtrip() {
        let pkt = build_fragment(
            (10, 0, 0, 1),
            (10, 0, 0, 2),
            17,
            0xABCD,
            0,
            false,
            vec![7; 100],
        );
        let parsed = parse_ipv4(&pkt).unwrap();
        assert!(parsed.checksum_valid);
        assert_eq!(parsed.header.src, Ipv4Addr::new(10, 0, 0, 1));
        assert_eq!(parsed.header.protocol, 17);
        assert_eq!(parsed.header.identification, 0xABCD);
        assert!(!parsed.header.is_fragment());
        assert_eq!(parsed.payload.len(), 100);
    }

    #[test]
    fn fragment_flags_and_offset() {
        let pkt = build_fragment((1, 2, 3, 4), (5, 6, 7, 8), 6, 1, 1480, true, vec![0; 1480]);
        let h = &parse_ipv4(&pkt).unwrap().header;
        assert!(h.mf);
        assert_eq!(h.fragment_offset, 1480);
        assert!(h.is_fragment());
    }

    #[test]
    fn truncated_and_malformed_are_distinct() {
        let err = parse_ipv4(&[0x45, 0, 0]).unwrap_err();
        assert!(matches!(err, Ipv4Error::Truncated { .. }), "{err:?}");

        let bad_version = [0x60u8; 20];
        assert!(matches!(
            parse_ipv4(&bad_version).unwrap_err(),
            Ipv4Error::Malformed { .. }
        ));

        let mut bad_ihl = vec![0x44u8]; // IHL=4
        bad_ihl.extend(std::iter::repeat_n(0u8, 19));
        assert!(matches!(
            parse_ipv4(&bad_ihl).unwrap_err(),
            Ipv4Error::Malformed { .. }
        ));
    }

    #[test]
    fn bad_checksum_is_reported_not_hard_rejected() {
        let mut pkt = build_fragment((1, 1, 1, 1), (2, 2, 2, 2), 17, 9, 0, false, vec![1]);
        pkt[11] = pkt[11].wrapping_add(1);
        let parsed = parse_ipv4(&pkt).unwrap();
        assert!(!parsed.checksum_valid);
    }
}
