//! 增量字节解析器与编码器（手写，不借用现成 MQTT 解析库）。
//!
//! `Decoder` 维护内部缓冲区，调用方任意粒度 `feed()` 字节流，
//! 反复调用 `next_packet()` 取出完整报文；数据不足时返回 `Ok(None)`。
//!
//! 长度上限：`max_packet_size` 限制“固定头 + 剩余长度”声明的总包长，
//! 超过即返回 `MqttError::PacketTooLarge`，在分配/拷贝大缓冲之前拒绝。

use crate::error::MqttError;
use crate::packet::{ConnAck, Connect, Packet, Publish, SubAck, Subscribe};
use crate::topic;

/// MQTT 3.1.1 协议允许的最大包长（剩余长度字段 4 字节上限）。
pub const MQTT_MAX_PACKET_SIZE: usize = 268_435_455;
/// 本实现默认上限：64 KiB，足够覆盖测试与小消息场景。
pub const DEFAULT_MAX_PACKET_SIZE: usize = 64 * 1024;

// 报文类型编号（固定头高 4 位）。
const T_CONNECT: u8 = 1;
const T_CONNACK: u8 = 2;
const T_PUBLISH: u8 = 3;
const T_PUBACK: u8 = 4;
const T_SUBSCRIBE: u8 = 8;
const T_SUBACK: u8 = 9;
const T_PINGREQ: u8 = 12;
const T_PINGRESP: u8 = 13;
const T_DISCONNECT: u8 = 14;

/// 增量解码器。
pub struct Decoder {
    buf: Vec<u8>,
    max_packet_size: usize,
}

impl Decoder {
    pub fn new(max_packet_size: usize) -> Self {
        assert!(max_packet_size <= MQTT_MAX_PACKET_SIZE);
        Decoder {
            buf: Vec::new(),
            max_packet_size,
        }
    }

    /// 追加字节流片段。
    pub fn feed(&mut self, data: &[u8]) {
        self.buf.extend_from_slice(data);
    }

    /// 尝试解析一个完整报文。数据不足返回 `Ok(None)`。
    pub fn next_packet(&mut self) -> Result<Option<Packet>, MqttError> {
        let Some((header, total)) = self.peek_frame()? else {
            return Ok(None);
        };
        if self.buf.len() < total {
            return Ok(None);
        }
        let frame: Vec<u8> = self.buf.drain(..total).collect();
        let body = &frame[header..];
        parse_packet(frame[0], body).map(Some)
    }

    /// 解析固定头，返回 (头部长度, 整包长度)；数据不足返回 None。
    fn peek_frame(&self) -> Result<Option<(usize, usize)>, MqttError> {
        if self.buf.is_empty() {
            return Ok(None);
        }
        // 剩余长度：1~4 字节变长编码。
        let mut remaining: usize = 0;
        let mut multiplier: usize = 1;
        let mut header_len = 1;
        let mut done = false;
        for i in 0..4 {
            match self.buf.get(1 + i) {
                None => return Ok(None), // 头还没收齐
                Some(&b) => {
                    header_len += 1;
                    remaining += ((b & 0x7f) as usize) * multiplier;
                    if b & 0x80 == 0 {
                        done = true;
                        break;
                    }
                    multiplier *= 128;
                }
            }
        }
        if !done {
            return Err(MqttError::Malformed("remaining length exceeds 4 bytes"));
        }
        let total = header_len + remaining;
        if total > self.max_packet_size {
            return Err(MqttError::PacketTooLarge {
                max: self.max_packet_size,
                got: total,
            });
        }
        Ok(Some((header_len, total)))
    }
}

// ---------- 解析 ----------

fn parse_packet(byte1: u8, body: &[u8]) -> Result<Packet, MqttError> {
    let ptype = byte1 >> 4;
    let flags = byte1 & 0x0f;
    // 固定头标志位校验（MQTT 3.1.1 表 2.2）。
    let expected: Option<u8> = match ptype {
        T_CONNECT | T_CONNACK | T_PUBACK | T_SUBACK | T_PINGREQ | T_PINGRESP | T_DISCONNECT => {
            Some(0)
        }
        T_SUBSCRIBE => Some(0b0010),
        T_PUBLISH => None, // 标志位承载 dup/qos/retain
        _ => None,
    };
    if let Some(exp) = expected {
        if flags != exp {
            return Err(MqttError::Malformed("invalid fixed-header flags"));
        }
    }
    match ptype {
        T_CONNECT => parse_connect(body).map(Packet::Connect),
        T_CONNACK => {
            if body.len() != 2 {
                return Err(MqttError::Malformed("connack length"));
            }
            if body[0] & !0x01 != 0 {
                return Err(MqttError::Malformed("connack reserved bits"));
            }
            Ok(Packet::ConnAck(ConnAck {
                session_present: body[0] & 1 == 1,
                return_code: body[1],
            }))
        }
        T_PUBLISH => parse_publish(flags, body).map(Packet::Publish),
        T_PUBACK => {
            let id = read_u16_exact(body)?;
            Ok(Packet::PubAck(id))
        }
        T_SUBSCRIBE => parse_subscribe(body).map(Packet::Subscribe),
        T_SUBACK => {
            if body.len() < 3 {
                return Err(MqttError::Malformed("suback length"));
            }
            let packet_id = u16::from_be_bytes([body[0], body[1]]);
            Ok(Packet::SubAck(SubAck {
                packet_id,
                granted: body[2..].to_vec(),
            }))
        }
        T_PINGREQ => {
            if !body.is_empty() {
                return Err(MqttError::Malformed("pingreq must be empty"));
            }
            Ok(Packet::PingReq)
        }
        T_PINGRESP => {
            if !body.is_empty() {
                return Err(MqttError::Malformed("pingresp must be empty"));
            }
            Ok(Packet::PingResp)
        }
        T_DISCONNECT => {
            if !body.is_empty() {
                return Err(MqttError::Malformed("disconnect must be empty"));
            }
            Ok(Packet::Disconnect)
        }
        0 => Err(MqttError::Malformed("reserved packet type 0")),
        15 => Err(MqttError::Malformed("reserved packet type 15")),
        other => Err(MqttError::UnsupportedPacketType(other)),
    }
}

/// 游标式读取器，避免手工维护偏移出错。
struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Cursor { buf, pos: 0 }
    }
    fn remaining(&self) -> &'a [u8] {
        &self.buf[self.pos..]
    }
    fn u8(&mut self) -> Result<u8, MqttError> {
        if self.pos >= self.buf.len() {
            return Err(MqttError::Malformed("unexpected end of packet"));
        }
        let b = self.buf[self.pos];
        self.pos += 1;
        Ok(b)
    }
    fn u16(&mut self) -> Result<u16, MqttError> {
        let hi = self.u8()?;
        let lo = self.u8()?;
        Ok(u16::from_be_bytes([hi, lo]))
    }
    fn bytes(&mut self) -> Result<Vec<u8>, MqttError> {
        let len = self.u16()? as usize;
        if self.remaining().len() < len {
            return Err(MqttError::Malformed("length-prefixed field overruns packet"));
        }
        let out = self.remaining()[..len].to_vec();
        self.pos += len;
        Ok(out)
    }
    fn utf8(&mut self) -> Result<String, MqttError> {
        let raw = self.bytes()?;
        String::from_utf8(raw).map_err(|_| MqttError::Malformed("invalid utf-8 string"))
    }
}

fn parse_connect(body: &[u8]) -> Result<Connect, MqttError> {
    let mut c = Cursor::new(body);
    let proto_name = c.utf8()?;
    let level = c.u8()?;
    if proto_name != "MQTT" || level != 4 {
        return Err(MqttError::UnsupportedProtocol {
            name: proto_name,
            level,
        });
    }
    let flags = c.u8()?;
    if flags & 0x01 != 0 {
        return Err(MqttError::Malformed("connect reserved flag bit set"));
    }
    let keep_alive = c.u16()?;
    let has_will = flags & 0x04 != 0;
    let will_qos = (flags >> 3) & 0x03;
    let will_retain = flags & 0x20 != 0;
    if !has_will && (will_qos != 0 || will_retain) {
        return Err(MqttError::Malformed("will flags set without will flag"));
    }
    if will_qos == 3 {
        return Err(MqttError::Malformed("will qos 3 is reserved"));
    }
    if will_qos == 2 {
        return Err(MqttError::UnsupportedQos(2));
    }
    let client_id = c.utf8()?;
    let will = if has_will {
        let topic = c.utf8()?;
        if !topic::valid_topic_name(&topic) {
            return Err(MqttError::Malformed("invalid will topic"));
        }
        let payload = c.bytes()?;
        Some(Publish {
            dup: false,
            qos: will_qos,
            retain: will_retain,
            topic,
            packet_id: None,
            payload,
        })
    } else {
        None
    };
    let username = if flags & 0x80 != 0 {
        Some(c.utf8()?)
    } else {
        None
    };
    let password = if flags & 0x40 != 0 {
        Some(c.bytes()?)
    } else {
        None
    };
    if !c.remaining().is_empty() {
        return Err(MqttError::Malformed("trailing bytes in connect"));
    }
    Ok(Connect {
        client_id,
        clean_session: flags & 0x02 != 0,
        keep_alive,
        will,
        username,
        password,
    })
}

fn parse_publish(flags: u8, body: &[u8]) -> Result<Publish, MqttError> {
    let dup = flags & 0x08 != 0;
    let qos = (flags >> 1) & 0x03;
    let retain = flags & 0x01 != 0;
    if qos == 3 {
        return Err(MqttError::Malformed("qos 3 is reserved"));
    }
    if qos == 2 {
        return Err(MqttError::UnsupportedQos(2));
    }
    let mut c = Cursor::new(body);
    let topic = c.utf8()?;
    if !topic::valid_topic_name(&topic) {
        return Err(MqttError::Malformed("invalid publish topic"));
    }
    let packet_id = if qos > 0 {
        let id = c.u16()?;
        if id == 0 {
            return Err(MqttError::Malformed("packet id must be non-zero"));
        }
        Some(id)
    } else {
        None
    };
    let payload = c.remaining().to_vec();
    Ok(Publish {
        dup,
        qos,
        retain,
        topic,
        packet_id,
        payload,
    })
}

fn parse_subscribe(body: &[u8]) -> Result<Subscribe, MqttError> {
    let mut c = Cursor::new(body);
    let packet_id = c.u16()?;
    if packet_id == 0 {
        return Err(MqttError::Malformed("packet id must be non-zero"));
    }
    let mut topics = Vec::new();
    while !c.remaining().is_empty() {
        let filter = c.utf8()?;
        let qos = c.u8()?;
        if qos > 2 {
            return Err(MqttError::Malformed("subscribe reserved qos bits set"));
        }
        if !topic::valid_filter(&filter) {
            return Err(MqttError::Malformed("invalid topic filter"));
        }
        topics.push((filter, qos));
    }
    if topics.is_empty() {
        return Err(MqttError::Malformed("subscribe with no topics"));
    }
    Ok(Subscribe { packet_id, topics })
}

fn read_u16_exact(body: &[u8]) -> Result<u16, MqttError> {
    if body.len() != 2 {
        return Err(MqttError::Malformed("expected 2-byte packet id"));
    }
    let id = u16::from_be_bytes([body[0], body[1]]);
    if id == 0 {
        return Err(MqttError::Malformed("packet id must be non-zero"));
    }
    Ok(id)
}

// ---------- 编码 ----------

/// 编码完整报文（固定头 + 可变头 + 载荷）。
pub fn encode(packet: &Packet) -> Vec<u8> {
    let mut body = Vec::new();
    let (ptype, flags) = match packet {
        Packet::Connect(c) => {
            put_utf8(&mut body, "MQTT");
            body.push(4);
            let mut flags = 0u8;
            if c.clean_session {
                flags |= 0x02;
            }
            if let Some(w) = &c.will {
                flags |= 0x04 | (w.qos << 3);
                if w.retain {
                    flags |= 0x20;
                }
            }
            if c.password.is_some() {
                flags |= 0x40;
            }
            if c.username.is_some() {
                flags |= 0x80;
            }
            body.push(flags);
            body.extend_from_slice(&c.keep_alive.to_be_bytes());
            put_utf8(&mut body, &c.client_id);
            if let Some(w) = &c.will {
                put_utf8(&mut body, &w.topic);
                put_bytes(&mut body, &w.payload);
            }
            if let Some(u) = &c.username {
                put_utf8(&mut body, u);
            }
            if let Some(p) = &c.password {
                put_bytes(&mut body, p);
            }
            (T_CONNECT, 0)
        }
        Packet::ConnAck(a) => {
            body.push(a.session_present as u8);
            body.push(a.return_code);
            (T_CONNACK, 0)
        }
        Packet::Publish(p) => {
            put_utf8(&mut body, &p.topic);
            if p.qos > 0 {
                let id = p.packet_id.expect("qos>0 publish needs packet id");
                body.extend_from_slice(&id.to_be_bytes());
            }
            body.extend_from_slice(&p.payload);
            let mut flags = p.qos << 1;
            if p.dup {
                flags |= 0x08;
            }
            if p.retain {
                flags |= 0x01;
            }
            (T_PUBLISH, flags)
        }
        Packet::PubAck(id) => {
            body.extend_from_slice(&id.to_be_bytes());
            (T_PUBACK, 0)
        }
        Packet::Subscribe(s) => {
            body.extend_from_slice(&s.packet_id.to_be_bytes());
            for (filter, qos) in &s.topics {
                put_utf8(&mut body, filter);
                body.push(*qos);
            }
            (T_SUBSCRIBE, 0b0010)
        }
        Packet::SubAck(s) => {
            body.extend_from_slice(&s.packet_id.to_be_bytes());
            body.extend_from_slice(&s.granted);
            (T_SUBACK, 0)
        }
        Packet::PingReq => (T_PINGREQ, 0),
        Packet::PingResp => (T_PINGRESP, 0),
        Packet::Disconnect => (T_DISCONNECT, 0),
    };
    let mut out = vec![(ptype << 4) | flags];
    encode_remaining_length(&mut out, body.len());
    out.extend_from_slice(&body);
    out
}

fn put_utf8(out: &mut Vec<u8>, s: &str) {
    out.extend_from_slice(&(s.len() as u16).to_be_bytes());
    out.extend_from_slice(s.as_bytes());
}

fn put_bytes(out: &mut Vec<u8>, b: &[u8]) {
    out.extend_from_slice(&(b.len() as u16).to_be_bytes());
    out.extend_from_slice(b);
}

fn encode_remaining_length(out: &mut Vec<u8>, mut len: usize) {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn decode_one(bytes: &[u8]) -> Result<Packet, MqttError> {
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        d.feed(bytes);
        d.next_packet().map(|p| p.expect("complete packet expected"))
    }

    #[test]
    fn roundtrip_all_packets() {
        let packets = vec![
            Packet::Connect(Connect {
                client_id: "c1".into(),
                clean_session: true,
                keep_alive: 60,
                will: Some(Publish::new("will/topic", 1, b"bye".to_vec())),
                username: Some("u".into()),
                password: Some(b"p".to_vec()),
            }),
            Packet::ConnAck(ConnAck {
                session_present: true,
                return_code: 0,
            }),
            Packet::Publish(Publish {
                dup: true,
                qos: 1,
                retain: true,
                topic: "a/b".into(),
                packet_id: Some(42),
                payload: b"hello".to_vec(),
            }),
            Packet::Publish(Publish::new("a/b", 0, b"q0".to_vec())),
            Packet::PubAck(7),
            Packet::Subscribe(Subscribe {
                packet_id: 9,
                topics: vec![("a/+".into(), 1), ("b/#".into(), 0)],
            }),
            Packet::SubAck(SubAck {
                packet_id: 9,
                granted: vec![1, 0],
            }),
            Packet::PingReq,
            Packet::PingResp,
            Packet::Disconnect,
        ];
        for p in packets {
            let bytes = encode(&p);
            let back = decode_one(&bytes).unwrap();
            assert_eq!(back, p, "roundtrip failed for {p:?}");
        }
    }

    #[test]
    fn incremental_byte_by_byte() {
        let p = Packet::Subscribe(Subscribe {
            packet_id: 1,
            topics: vec![("sensors/+".into(), 1)],
        });
        let bytes = encode(&p);
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        let mut got = None;
        for b in &bytes {
            d.feed(&[*b]);
            if let Some(pkt) = d.next_packet().unwrap() {
                got = Some(pkt);
            }
        }
        assert_eq!(got, Some(p));
    }

    #[test]
    fn two_packets_in_one_buffer() {
        let mut bytes = encode(&Packet::PingReq);
        bytes.extend_from_slice(&encode(&Packet::Disconnect));
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        d.feed(&bytes);
        assert_eq!(d.next_packet().unwrap(), Some(Packet::PingReq));
        assert_eq!(d.next_packet().unwrap(), Some(Packet::Disconnect));
        assert_eq!(d.next_packet().unwrap(), None);
    }

    #[test]
    fn packet_too_large_rejected_before_buffering() {
        // 声明剩余长度 100_000，上限 1024：只喂头就应立即报错。
        let mut d = Decoder::new(1024);
        d.feed(&[0x30, 0xA0, 0x8D, 0x06]); // PUBLISH, remaining = 100_000
        assert_eq!(
            d.next_packet(),
            Err(MqttError::PacketTooLarge {
                max: 1024,
                got: 4 + 100_000
            })
        );
    }

    #[test]
    fn malformed_remaining_length() {
        let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
        d.feed(&[0x30, 0x80, 0x80, 0x80, 0x80]); // 4 个延续位字节
        assert_eq!(
            d.next_packet(),
            Err(MqttError::Malformed("remaining length exceeds 4 bytes"))
        );
    }

    #[test]
    fn bad_flags_rejected() {
        // SUBSCRIBE 必须带 0b0010 标志。
        let mut bad = encode(&Packet::Subscribe(Subscribe {
            packet_id: 1,
            topics: vec![("a".into(), 0)],
        }));
        bad[0] = 0x80;
        assert_eq!(
            decode_one(&bad),
            Err(MqttError::Malformed("invalid fixed-header flags"))
        );
    }

    #[test]
    fn unsupported_types_and_qos() {
        assert_eq!(
            decode_one(&[0x50, 0x02, 0x00, 0x01]), // PUBREC
            Err(MqttError::UnsupportedPacketType(5))
        );
        assert_eq!(
            decode_one(&[0xA2, 0x02, 0x00, 0x01]), // UNSUBSCRIBE
            Err(MqttError::UnsupportedPacketType(10))
        );
        // QoS2 PUBLISH
        let mut p = encode(&Packet::Publish(Publish {
            dup: false,
            qos: 1,
            retain: false,
            topic: "a".into(),
            packet_id: Some(1),
            payload: vec![],
        }));
        p[0] = 0x34; // qos=2
        assert_eq!(decode_one(&p), Err(MqttError::UnsupportedQos(2)));
        // QoS3 PUBLISH（保留值）
        p[0] = 0x36;
        assert_eq!(
            decode_one(&p),
            Err(MqttError::Malformed("qos 3 is reserved"))
        );
    }

    #[test]
    fn connect_wrong_protocol() {
        let mut body = Vec::new();
        put_utf8(&mut body, "MQIsdp");
        body.push(3); // MQTT 3.1
        body.push(0x02);
        body.extend_from_slice(&60u16.to_be_bytes());
        put_utf8(&mut body, "x");
        let mut bytes = vec![0x10];
        encode_remaining_length(&mut bytes, body.len());
        bytes.extend_from_slice(&body);
        assert!(matches!(
            decode_one(&bytes),
            Err(MqttError::UnsupportedProtocol { .. })
        ));
    }
}
