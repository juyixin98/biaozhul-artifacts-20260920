//! 从零实现的 MQTT 3.1.1 增量字节解析器与编码器（纯 `std`，无第三方依赖）。
//!
//! ## 增量解析
//! TCP 是字节流，一个 MQTT 报文可能：
//! - 横跨多次 `read()`（只到了一半）；
//! - 与后续报文粘在同一次 `read()` 里。
//!
//! [`Decoder`] 内部维护一个增长缓冲：每次从 socket 读到的字节先 [`Decoder::feed`]
//! 进去，再循环调用 [`Decoder::try_parse`]：
//! - 完整报文：返回 `Ok(Some(Packet))`，解析过的字节被丢弃，可继续解析下一个；
//! - 字节不够：返回 `Ok(None)`，等待更多字节，缓冲中已有字节原样保留；
//! - 协议错误：返回 [`CodecError`]，按 §4.13 关闭连接。
//!
//! ## 长度上限
//! 剩余长度超过 [`Decoder::new`] 配置的 `max_packet_size` 时返回
//! [`CodecError::PayloadTooLarge`]，不会无界分配内存。
//!
//! 解析出的 [`Packet`] 以 `&[u8]`/`&str` 借用输入缓冲，是零拷贝视图；
//! 需要长期持有时由调用方 `to_vec()` / `to_owned()`。

use crate::error::CodecError;
use crate::packet::{Connect, FixedHeader, Packet, PacketType, Publish};

/// MQTT 剩余长度理论最大值（4 字节可变长编码，MQTT 3.1.1 §2.2.3）。
pub const MAX_REMAINING_LENGTH: usize = 268_435_455;

/// 本子集默认的单包剩余长度上限（256 KiB），远小于协议理论上限。
pub const DEFAULT_MAX_PACKET_SIZE: usize = 256 * 1024;

/// 剩余长度编码最多占 4 字节。
const MAX_LEN_BYTES: usize = 4;

/// 增量解析器。
#[derive(Debug)]
pub struct Decoder {
    buf: Vec<u8>,
    max_packet_size: usize,
}

impl Decoder {
    pub fn new(max_packet_size: usize) -> Self {
        Self {
            buf: Vec::new(),
            max_packet_size,
        }
    }

    /// 当前缓冲中尚未解析的字节数。
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }

    /// 追加从 TCP 流新读到的字节。
    pub fn feed(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    /// 尝试从缓冲头部解析一个完整报文。
    ///
    /// 返回 `Ok(None)` 表示当前字节不足（或缓冲为空），调用方应继续读 socket。
    /// 返回 `Ok(Some(pkt))` 后内部自动丢弃已消费的字节（包括其后的粘包数据保持不动）。
    pub fn try_parse(&mut self) -> Result<Option<Packet>, CodecError> {
        if self.buf.is_empty() {
            return Ok(None);
        }

        // 阶段 1：解析固定头（类型 + 标志 + 剩余长度）。
        let (header, header_len) = match parse_fixed_header(&self.buf, self.max_packet_size)? {
            Some(v) => v,
            None => return Ok(None),
        };

        let total_len = header_len + header.remaining_length;
        if self.buf.len() < total_len {
            // 报文体尚未到齐；保留缓冲等待更多字节。
            return Ok(None);
        }

        // 阶段 2：只在完整报文的字节范围内解析报文体。
        let result;
        {
            let body = &self.buf[header_len..total_len];
            result = parse_body(&header, body);
        }

        // 阶段 3：无论解析成功与否都丢弃本报文消费掉的字节（错误时调用方
        // 会直接关连接；成功时保留可能存在的粘包数据）。
        if result.is_ok() {
            // 直接 truncate 起始位置：本报文在缓冲头部，drain 语义等价但更快。
            self.buf.copy_within(total_len.., 0);
            self.buf.truncate(self.buf.len() - total_len);
        }

        result.map(Some)
    }
}

/// 解析固定头；字节不足时返回 `Ok(None)`。
fn parse_fixed_header(
    data: &[u8],
    max_packet_size: usize,
) -> Result<Option<(FixedHeader, usize)>, CodecError> {
    let first = match data.first() {
        Some(&b) => b,
        None => return Ok(None),
    };
    let type_bits = first >> 4;
    let flags = first & 0x0F;
    let packet_type =
        PacketType::from_u8(type_bits).ok_or(CodecError::InvalidPacketType(type_bits))?;

    // 解码剩余长度（1–4 字节可变长整数）。
    let mut multiplier: usize = 1;
    let mut remaining_length: usize = 0;
    let mut len_bytes = 0usize;
    loop {
        let idx = 1 + len_bytes;
        let Some(&b) = data.get(idx) else {
            // 长度字段还没收全。
            return Ok(None);
        };
        len_bytes += 1;
        remaining_length = remaining_length
            .checked_add(((b & 0x7F) as usize) * multiplier)
            .ok_or(CodecError::MalformedRemainingLength)?;
        if b & 0x80 == 0 {
            break;
        }
        if len_bytes == MAX_LEN_BYTES {
            // 第 4 字节的 continuation bit 必须为 0，且值不得超过 0x7F。
            return Err(CodecError::MalformedRemainingLength);
        }
        multiplier *= 128;
    }

    if remaining_length > MAX_REMAINING_LENGTH {
        return Err(CodecError::MalformedRemainingLength);
    }
    if remaining_length > max_packet_size {
        return Err(CodecError::PayloadTooLarge {
            limit: max_packet_size,
            actual: remaining_length,
        });
    }

    Ok(Some((
        FixedHeader {
            packet_type,
            flags,
            remaining_length,
        },
        1 + len_bytes,
    )))
}

/// 只读游标，便于在报文体切片中顺序读取字段。
struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, pos: 0 }
    }

    fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    fn read_u8(&mut self) -> Result<u8, CodecError> {
        let v = *self.data.get(self.pos).ok_or(CodecError::NeedMoreData)?;
        self.pos += 1;
        Ok(v)
    }

    fn read_u16(&mut self) -> Result<u16, CodecError> {
        if self.remaining() < 2 {
            return Err(CodecError::NeedMoreData);
        }
        let v = u16::from_be_bytes([self.data[self.pos], self.data[self.pos + 1]]);
        self.pos += 2;
        Ok(v)
    }

    /// 读取 MQTT 二进制字符串（2 字节长度前缀 + UTF-8 字节），校验通过后转为 owned。
    fn read_string(&mut self) -> Result<String, CodecError> {
        let len = self.read_u16()? as usize;
        if self.remaining() < len {
            return Err(CodecError::LengthExceedsBuffer {
                wanted: len,
                available: self.remaining(),
            });
        }
        let s = std::str::from_utf8(&self.data[self.pos..self.pos + len])
            .map_err(|_| CodecError::InvalidUtf8)?
            .to_owned();
        self.pos += len;
        Ok(s)
    }

    /// 读取二进制数据（如 password，不要求 UTF-8）。
    fn read_vec(&mut self) -> Result<Vec<u8>, CodecError> {
        let len = self.read_u16()? as usize;
        if self.remaining() < len {
            return Err(CodecError::LengthExceedsBuffer {
                wanted: len,
                available: self.remaining(),
            });
        }
        let v = self.data[self.pos..self.pos + len].to_vec();
        self.pos += len;
        Ok(v)
    }

    /// 读取 UTF-8 字符串并仅在内部借用（用于协议名等无需持有的短字段）。
    fn read_str(&mut self) -> Result<&'a str, CodecError> {
        let len = self.read_u16()? as usize;
        if self.remaining() < len {
            return Err(CodecError::LengthExceedsBuffer {
                wanted: len,
                available: self.remaining(),
            });
        }
        let s = std::str::from_utf8(&self.data[self.pos..self.pos + len])
            .map_err(|_| CodecError::InvalidUtf8)?;
        self.pos += len;
        Ok(s)
    }
}

/// 按报文类型解析报文体；`body.len()` 一定等于 `header.remaining_length`。
fn parse_body(header: &FixedHeader, body: &[u8]) -> Result<Packet, CodecError> {
    let mut cur = Cursor::new(body);
    match header.packet_type {
        PacketType::Connect => parse_connect(header, &mut cur).map(Packet::Connect),
        PacketType::Publish => parse_publish(header, &mut cur).map(Packet::Publish),
        PacketType::Puback => {
            if header.flags != 0b0000 {
                return Err(CodecError::InvalidReservedFlag("PUBACK"));
            }
            let id = cur.read_u16()?;
            if id == 0 {
                return Err(CodecError::MalformedPacket(
                    "PUBACK packet id must not be 0",
                ));
            }
            ensure_consumed(&cur, "PUBACK body must be exactly 2 bytes")?;
            Ok(Packet::Puback(id))
        }
        PacketType::Subscribe => parse_subscribe(header, &mut cur),
        PacketType::Pingreq => {
            if header.flags != 0b0000 || header.remaining_length != 0 {
                return Err(CodecError::MalformedPacket(
                    "PINGREQ must be empty with flags 0",
                ));
            }
            Ok(Packet::Pingreq)
        }
        PacketType::Disconnect => {
            if header.flags != 0b0000 || header.remaining_length != 0 {
                return Err(CodecError::MalformedPacket(
                    "DISCONNECT must be empty with flags 0",
                ));
            }
            Ok(Packet::Disconnect)
        }
        // 服务端出站类型，入站解析器不接受。
        PacketType::Connack | PacketType::Suback | PacketType::Pingresp => {
            Err(CodecError::InvalidPacketType(header.packet_type.as_u8()))
        }
    }
}

fn ensure_consumed(cur: &Cursor<'_>, what: &'static str) -> Result<(), CodecError> {
    if cur.remaining() != 0 {
        return Err(CodecError::MalformedPacket(what));
    }
    Ok(())
}

/// CONNECT（MQTT 3.1.1 §3.1）。
fn parse_connect(header: &FixedHeader, cur: &mut Cursor<'_>) -> Result<Connect, CodecError> {
    if header.flags != 0b0000 {
        return Err(CodecError::InvalidReservedFlag("CONNECT"));
    }

    // 可变头：协议名（借用切片比较即可）。
    let proto = cur.read_str()?;
    if proto != "MQTT" {
        return Err(CodecError::InvalidProtocolName);
    }
    // 协议级别：MQTT 3.1.1 = 4。
    let level = cur.read_u8()?;
    if level != 4 {
        return Err(CodecError::UnsupportedProtocolLevel(level));
    }

    let connect_flags = cur.read_u8()?;
    // bit0 保留，必须为 0（§3.1.2.3）。
    if connect_flags & 0b0000_0001 != 0 {
        return Err(CodecError::InvalidReservedFlag("CONNECT flags bit0"));
    }
    let clean_session = connect_flags & 0b0000_0010 != 0;
    let will_flag = connect_flags & 0b0000_0100 != 0;
    let will_qos = (connect_flags >> 3) & 0b11;
    let will_retain = connect_flags & 0b0010_0000 != 0;
    let password_flag = connect_flags & 0b0100_0000 != 0;
    let username_flag = connect_flags & 0b1000_0000 != 0;

    if will_qos > 2 {
        return Err(CodecError::InvalidQoS(will_qos));
    }
    // Will QoS/Retain 只允许在 Will Flag 置位时出现（§3.1.2.5/6/7 一致性要求）。
    if !will_flag && (will_qos != 0 || will_retain) {
        return Err(CodecError::MalformedConnect(
            "will QoS/retain set without Will Flag",
        ));
    }

    let keep_alive_secs = cur.read_u16()?;

    // 载荷（严格按 §3.1.3 顺序：ClientId, WillTopic, WillMessage, UserName, Password）。
    let client_id = cur.read_string()?;

    // Will 字段仍按协议解析（以便正确跳过），但服务端随后会以 0x03 拒绝。
    if will_flag {
        let _will_topic = cur.read_string()?;
        let _will_message = cur.read_vec()?;
    }

    let username = if username_flag {
        Some(cur.read_string()?)
    } else {
        None
    };
    let password = if password_flag {
        Some(cur.read_vec()?)
    } else {
        None
    };

    if cur.remaining() != 0 {
        return Err(CodecError::MalformedConnect("trailing bytes in CONNECT"));
    }

    Ok(Connect {
        clean_session,
        keep_alive_secs,
        client_id,
        username,
        password,
        will_flag,
        will_retain,
        will_qos,
    })
}

/// PUBLISH（§3.3）。
fn parse_publish(header: &FixedHeader, cur: &mut Cursor<'_>) -> Result<Publish, CodecError> {
    let dup = header.dup();
    let qos = header.qos();
    if qos == 3 {
        return Err(CodecError::InvalidQoS(3));
    }
    let retain = header.retain();

    let topic = cur.read_string()?;
    if !crate::topic::is_valid_topic_name(&topic) {
        return Err(CodecError::InvalidTopicName);
    }

    let packet_id = if qos > 0 {
        let id = cur.read_u16()?;
        if id == 0 {
            return Err(CodecError::MalformedPacket(
                "PUBLISH packet id must not be 0",
            ));
        }
        Some(id)
    } else {
        None
    };

    // 载荷为报文体内剩余的全部字节（可以为空）。
    let payload = cur.data[cur.pos..].to_vec();

    Ok(Publish {
        topic,
        packet_id,
        payload,
        qos,
        dup,
        retain,
    })
}

/// SUBSCRIBE（§3.8）：固定头低 4 位固定为 0b0010，至少含一个过滤器。
fn parse_subscribe(header: &FixedHeader, cur: &mut Cursor<'_>) -> Result<Packet, CodecError> {
    if header.flags != 0b0010 {
        return Err(CodecError::InvalidReservedFlag(
            "SUBSCRIBE low bits must be 0010",
        ));
    }
    let packet_id = cur.read_u16()?;
    if packet_id == 0 {
        return Err(CodecError::MalformedPacket(
            "SUBSCRIBE packet id must not be 0",
        ));
    }

    let mut filters: Vec<(String, u8)> = Vec::new();
    while cur.remaining() > 0 {
        let filter = cur.read_string()?;
        let qos = cur.read_u8()?;
        // 请求 QoS：只允许 0/1/2（§3.8.3.1），本子集最高授予 QoS1。
        if qos > 2 {
            return Err(CodecError::InvalidQoS(qos));
        }
        if !crate::topic::is_valid_topic_filter(&filter) {
            return Err(CodecError::InvalidTopicFilter);
        }
        filters.push((filter, qos));
    }
    if filters.is_empty() {
        return Err(CodecError::EmptySubscriptionList);
    }

    Ok(Packet::Subscribe(packet_id, filters))
}

// ---------------------------------------------------------------------------
// 编码器（出站方向）：全部为本机构造的合法报文，出错只可能是分配失败，故直接
// 返回 Vec<u8>。
// ---------------------------------------------------------------------------

/// 编码剩余长度（可变长整数）。
pub fn encode_remaining_length(mut len: usize, out: &mut Vec<u8>) {
    loop {
        let mut b = (len % 128) as u8;
        len /= 128;
        if len > 0 {
            b |= 0x80;
        }
        out.push(b);
        if len == 0 {
            break;
        }
    }
}

fn write_str(out: &mut Vec<u8>, s: &str) {
    out.extend_from_slice(&(s.len() as u16).to_be_bytes());
    out.extend_from_slice(s.as_bytes());
}

/// CONNACK（§3.2）：固定 4 字节。
pub fn encode_connack(session_present: bool, reason: u8) -> Vec<u8> {
    vec![
        0x20, // CONNACK
        0x02, // 剩余长度 2
        if session_present { 0x01 } else { 0x00 },
        reason,
    ]
}

/// PINGRESP（§3.13）。
pub fn encode_pingresp() -> Vec<u8> {
    vec![0xD0, 0x00]
}

/// PUBACK（§3.4）。
pub fn encode_puback(packet_id: u16) -> Vec<u8> {
    vec![0x40, 0x02, (packet_id >> 8) as u8, (packet_id & 0xFF) as u8]
}

/// SUBACK（§3.9）。
pub fn encode_suback(packet_id: u16, return_codes: &[u8]) -> Vec<u8> {
    let mut body = Vec::with_capacity(2 + return_codes.len());
    body.extend_from_slice(&packet_id.to_be_bytes());
    body.extend_from_slice(return_codes);

    let mut out = Vec::with_capacity(2 + body.len());
    out.push(0x90);
    encode_remaining_length(body.len(), &mut out);
    out.extend_from_slice(&body);
    out
}

/// PUBLISH（§3.3）出站。`dup` 用于重发时置位 DUP；qos 仅允许 0/1。
pub fn encode_publish(
    topic: &str,
    packet_id: Option<u16>,
    payload: &[u8],
    qos: u8,
    dup: bool,
    retain: bool,
) -> Vec<u8> {
    debug_assert!(qos <= 1, "subset broker only publishes QoS0/QoS1");
    debug_assert!(qos == 0 || packet_id.is_some());

    let mut first: u8 = 0x30; // PUBLISH type
    if dup {
        first |= 0b1000;
    }
    first |= (qos & 0b11) << 1;
    if retain {
        first |= 0b0001;
    }

    let mut body = Vec::with_capacity(topic.len() + 2 + 2 + payload.len());
    write_str(&mut body, topic);
    if let Some(id) = packet_id {
        body.extend_from_slice(&id.to_be_bytes());
    }
    body.extend_from_slice(payload);

    let mut out = Vec::with_capacity(1 + 4 + body.len());
    out.push(first);
    encode_remaining_length(body.len(), &mut out);
    out.extend_from_slice(&body);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remaining_length_roundtrip_boundaries() {
        for len in [
            0usize,
            1,
            127,
            128,
            16383,
            16384,
            2_097_151,
            2_097_152,
            268_435_455,
        ] {
            let mut out = Vec::new();
            encode_remaining_length(len, &mut out);
            let mut d = Decoder::new(MAX_REMAINING_LENGTH);
            d.feed(&[0x30]); // 先放一个类型字节占位
            d.feed(&out);
            // 手工验证解码值
            let (header, hlen) = parse_fixed_header(&d.buf, MAX_REMAINING_LENGTH)
                .unwrap()
                .unwrap();
            assert_eq!(header.remaining_length, len);
            assert_eq!(hlen, 1 + out.len());
        }
    }

    #[test]
    fn incomplete_remaining_length_returns_need_more() {
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        d.feed(&[0x30, 0x80]);
        assert!(d.try_parse().unwrap().is_none());
        d.feed(&[0x01]);
        // 现在长度字段完整（128），但报文体还没到：仍需更多数据。
        assert!(d.try_parse().unwrap().is_none());
    }

    #[test]
    fn remaining_length_fifth_byte_rejected() {
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        d.feed(&[0x30, 0x80, 0x80, 0x80, 0x80, 0x00]);
        assert_eq!(
            d.try_parse().unwrap_err(),
            CodecError::MalformedRemainingLength
        );
    }

    #[test]
    fn packet_size_limit_enforced() {
        let mut d = Decoder::new(10);
        let mut bytes = vec![0x30, 0x0B]; // PUBLISH, remaining 11
        bytes.extend_from_slice(&[0u8; 11]);
        d.feed(&bytes);
        assert!(matches!(
            d.try_parse(),
            Err(CodecError::PayloadTooLarge {
                limit: 10,
                actual: 11
            })
        ));
    }

    #[test]
    fn reserved_packet_types_rejected() {
        for t in [0u8, 15] {
            let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
            d.feed(&[t << 4, 0x00]);
            assert!(matches!(
                d.try_parse(),
                Err(CodecError::InvalidPacketType(_))
            ));
        }
    }
}
