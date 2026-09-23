//! 集成测试公共工具：启动临时 broker、构造原始 MQTT 字节、读取并解析帧。

#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use mqtt_subset::broker::{Broker, BrokerConfig};
use mqtt_subset::codec::encode_remaining_length;

/// 启动一个绑定随机端口、静默、快速重发的 broker。
pub fn start_test_broker(retry_ms: u64) -> Broker {
    Broker::start(BrokerConfig {
        bind_addr: "127.0.0.1:0".to_string(),
        max_packet_size: 256 * 1024,
        retry_interval: Duration::from_millis(retry_ms),
        keepalive_multiplier: 1.5,
        quiet: true,
    })
    .expect("broker start")
}

/// 建立 TCP 连接（尚未发送 CONNECT）。
pub fn raw_connect(broker: &Broker) -> TcpStream {
    let addr = broker.local_addr().unwrap();
    let s = TcpStream::connect(addr).unwrap();
    s.set_read_timeout(Some(Duration::from_millis(800)))
        .unwrap();
    s.set_nodelay(true).unwrap();
    s
}

/// 把变长字段（string/binary）写入缓冲。
pub fn write_mlstr(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
    out.extend_from_slice(bytes);
}

/// 构造 CONNECT 帧。
///
/// - `clean`：CleanSession 位；
/// - `level`：协议级别（4 = MQTT 3.1.1）；
/// - `will`：附带 Will 字段（用于测试子集拒绝路径）。
pub fn connect_frame(
    client_id: &str,
    clean: bool,
    keep_alive: u16,
    level: u8,
    will: bool,
) -> Vec<u8> {
    let mut body = Vec::new();
    write_mlstr(&mut body, b"MQTT");
    body.push(level);
    let mut flags = 0u8;
    if clean {
        flags |= 0x02;
    }
    if will {
        flags |= 0b0000_0100; // Will Flag, QoS0, no retain
    }
    body.push(flags);
    body.extend_from_slice(&keep_alive.to_be_bytes());
    write_mlstr(&mut body, client_id.as_bytes());
    if will {
        write_mlstr(&mut body, b"will/topic");
        write_mlstr(&mut body, b"bye");
    }

    let mut frame = vec![0x10];
    encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    frame
}

/// 读取并断言 CONNACK，返回 (session_present, return_code)。
pub fn read_connack(s: &mut TcpStream) -> (bool, u8) {
    let f = read_frame(s);
    assert_eq!(f.first, 0x20, "expected CONNACK, got {:#x}", f.first);
    assert_eq!(f.body.len(), 2);
    (f.body[0] == 1, f.body[1])
}

pub fn send_connect(s: &mut TcpStream, id: &str, clean: bool) -> (bool, u8) {
    s.write_all(&connect_frame(id, clean, 60, 4, false))
        .unwrap();
    read_connack(s)
}

/// 构造 PUBLISH 帧。
pub fn publish_frame(
    topic: &str,
    id: Option<u16>,
    payload: &[u8],
    qos: u8,
    dup: bool,
    retain: bool,
) -> Vec<u8> {
    let mut first = 0x30u8;
    if dup {
        first |= 0b1000;
    }
    first |= (qos & 0b11) << 1;
    if retain {
        first |= 0b0001;
    }
    let mut body = Vec::new();
    write_mlstr(&mut body, topic.as_bytes());
    if let Some(id) = id {
        body.extend_from_slice(&id.to_be_bytes());
    }
    body.extend_from_slice(payload);

    let mut frame = vec![first];
    encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    frame
}

/// 构造 PUBACK 帧。
pub fn puback_frame(id: u16) -> Vec<u8> {
    vec![0x40, 0x02, (id >> 8) as u8, (id & 0xFF) as u8]
}

/// 构造 SUBSCRIBE 帧；filters: (过滤器, QoS)。
pub fn subscribe_frame(packet_id: u16, filters: &[(&str, u8)]) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(&packet_id.to_be_bytes());
    for (f, q) in filters {
        write_mlstr(&mut body, f.as_bytes());
        body.push(*q);
    }
    let mut frame = vec![0x82];
    encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    frame
}

pub fn pingreq_frame() -> Vec<u8> {
    vec![0xC0, 0x00]
}

pub fn disconnect_frame() -> Vec<u8> {
    vec![0xE0, 0x00]
}

/// 读取到的一帧：(首字节, 报文体)。
pub struct Frame {
    pub first: u8,
    pub body: Vec<u8>,
}

/// 读取一个完整 MQTT 帧；超时/EOF 返回错误。
pub fn read_frame(s: &mut TcpStream) -> Frame {
    let first = read_u8(s);
    let mut multiplier = 1usize;
    let mut remaining = 0usize;
    for _ in 0..4 {
        let b = read_u8(s);
        remaining += (b as usize & 0x7F) * multiplier;
        if b & 0x80 == 0 {
            let mut body = vec![0u8; remaining];
            s.read_exact(&mut body).expect("read frame body");
            return Frame { first, body };
        }
        multiplier *= 128;
    }
    panic!("remaining length too long");
}

/// 非致命读：超时时 panic 信息由调用测试命名。
fn read_u8(s: &mut TcpStream) -> u8 {
    let mut b = [0u8; 1];
    s.read_exact(&mut b).expect("read one byte (frame header)");
    b[0]
}

/// 在 `first` 上解释一帧 PUBLISH，返回 (topic, id, payload, qos, dup, retain)。
pub fn parse_publish_frame(f: &Frame) -> (String, Option<u16>, Vec<u8>, u8, bool, bool) {
    let first = f.first;
    assert_eq!(first >> 4, 3, "expected PUBLISH");
    let qos = (first >> 1) & 0b11;
    let dup = first & 0b1000 != 0;
    let retain = first & 0b0001 != 0;
    let body = &f.body;
    let tlen = u16::from_be_bytes([body[0], body[1]]) as usize;
    let topic = String::from_utf8(body[2..2 + tlen].to_vec()).unwrap();
    let mut pos = 2 + tlen;
    let id = if qos > 0 {
        let id = u16::from_be_bytes([body[pos], body[pos + 1]]);
        pos += 2;
        Some(id)
    } else {
        None
    };
    let payload = body[pos..].to_vec();
    (topic, id, payload, qos, dup, retain)
}

/// 解释 SUBACK：(packet_id, 返回码列表)。
pub fn parse_suback(f: &Frame) -> (u16, Vec<u8>) {
    assert_eq!(f.first >> 4, 9);
    let id = u16::from_be_bytes([f.body[0], f.body[1]]);
    (id, f.body[2..].to_vec())
}

/// 解析 PUBACK 帧的包标识符。
pub fn parse_puback(f: &Frame) -> u16 {
    assert_eq!(f.first >> 4, 4);
    u16::from_be_bytes([f.body[0], f.body[1]])
}

/// 尝试读一帧，超时返回 None（不 panic），便于断言「不应收到任何消息」。
pub fn try_read_frame(s: &mut TcpStream) -> Option<Frame> {
    s.set_read_timeout(Some(Duration::from_millis(300)))
        .unwrap();
    let first = read_u8_opt(s)?;
    let mut multiplier = 1usize;
    let mut remaining = 0usize;
    for _ in 0..4 {
        let b = read_u8_opt(s)?;
        remaining += (b as usize & 0x7F) * multiplier;
        if b & 0x80 == 0 {
            let mut body = vec![0u8; remaining];
            s.read_exact(&mut body).ok()?;
            s.set_read_timeout(Some(Duration::from_millis(800)))
                .unwrap();
            return Some(Frame { first, body });
        }
        multiplier *= 128;
    }
    None
}

fn read_u8_opt(s: &mut TcpStream) -> Option<u8> {
    let mut b = [0u8; 1];
    match s.read_exact(&mut b) {
        Ok(()) => Some(b[0]),
        Err(_) => None,
    }
}

/// 短暂等待，给后台重发/路由线程留出调度时间。
pub fn sleep_ms(ms: u64) {
    std::thread::sleep(Duration::from_millis(ms));
}
