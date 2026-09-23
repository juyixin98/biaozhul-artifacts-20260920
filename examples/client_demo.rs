//! 演示用极简 MQTT 3.1.1 子集客户端（纯标准库）。
//!
//! 用法：
//!   # 订阅者（先在另一终端启动 broker 与订阅者）
//!   cargo run --example client_demo -- sub  127.0.0.1:1883 sub1 "demo/+" 0
//!   # 发布 QoS1 消息
//!   cargo run --example client_demo -- pub  127.0.0.1:1883 pub1 "demo/temp" 1 "hello qos1"
//!
//! 行为：
//! - 发布端 CleanSession=1；订阅端 CleanSession=0（可演示持久会话恢复）；
//! - 发布端 QoS1 发送后等待 PUBACK（读超时 5 秒）；
//! - 订阅端收到 QoS1 PUBLISH 后自动回 PUBACK，并打印 DUP/RETAIN 标志。
//!
//! 注意：库内的 [`mqtt_subset::codec::Decoder`] 是「服务端入站」解析器，
//! 按 MQTT 角色会拒绝 SUBACK/CONNACK 等服务端出站报文。因此本示例不依赖它，
//! 只演示手工读取帧；生产客户端应使用独立的客户端解析器。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 4 {
        eprintln!("usage: client_demo <pub|sub> <addr> <client_id> <topic> [qos] [payload]");
        std::process::exit(2);
    }
    let mode = &args[0];
    let addr = &args[1];
    let client_id = &args[2];
    let topic = &args[3];
    let qos: u8 = args.get(4).and_then(|x| x.parse().ok()).unwrap_or(0);

    let mut stream = TcpStream::connect(addr).expect("connect failed");
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    let clean = mode == "pub";
    send_connect(&mut stream, client_id, clean);
    let connack = read_exact_n(&mut stream, 4).expect("read CONNACK");
    assert_eq!(connack[0] >> 4, 2, "expected CONNACK");
    println!(
        "CONNACK: session_present={} return_code={}",
        connack[2], connack[3]
    );
    assert_eq!(connack[3], 0, "broker rejected CONNECT");

    match mode.as_str() {
        "pub" => {
            let payload = args.get(5).map(|s| s.as_bytes()).unwrap_or(b"");
            do_publish(&mut stream, topic, qos, payload);
        }
        "sub" => do_subscribe(&mut stream, topic, qos),
        other => panic!("unknown mode {other}"),
    }
}

fn send_connect(stream: &mut TcpStream, client_id: &str, clean_session: bool) {
    let mut body = Vec::new();
    body.extend_from_slice(&4u16.to_be_bytes());
    body.extend_from_slice(b"MQTT");
    body.push(4); // 协议级别 3.1.1
    body.push(if clean_session { 0x02 } else { 0x00 });
    body.extend_from_slice(&60u16.to_be_bytes());
    body.extend_from_slice(&(client_id.len() as u16).to_be_bytes());
    body.extend_from_slice(client_id.as_bytes());

    let mut frame = vec![0x10];
    mqtt_subset::codec::encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    stream.write_all(&frame).unwrap();
}

fn do_publish(stream: &mut TcpStream, topic: &str, qos: u8, payload: &[u8]) {
    let frame = mqtt_subset::codec::encode_publish(
        topic,
        (qos > 0).then_some(1000),
        payload,
        qos,
        false,
        false,
    );
    stream.write_all(&frame).unwrap();
    println!(
        "PUBLISH sent: topic={topic} qos={qos} wire_bytes={}",
        frame.len()
    );

    if qos == 1 {
        let (first, body) = read_frame(stream).expect("awaiting frame");
        assert_eq!(first >> 4, 4, "expected PUBACK");
        let id = u16::from_be_bytes([body[0], body[1]]);
        assert_eq!(id, 1000);
        println!("PUBACK received: id={id}");
    }
}

fn do_subscribe(stream: &mut TcpStream, topic: &str, qos: u8) {
    // SUBSCRIBE 固定头 0x82（类型8 + 低4位保留 0010）。
    let mut body = Vec::new();
    body.extend_from_slice(&2000u16.to_be_bytes());
    body.extend_from_slice(&(topic.len() as u16).to_be_bytes());
    body.extend_from_slice(topic.as_bytes());
    body.push(qos);
    let mut frame = vec![0x82];
    mqtt_subset::codec::encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    stream.write_all(&frame).unwrap();
    println!("SUBSCRIBE sent: filter={topic} qos={qos}");

    loop {
        let (first, body) = match read_frame(stream) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                println!("connection closed");
                break;
            }
            Err(e) => {
                eprintln!("read error: {e}");
                break;
            }
        };
        let ptype = first >> 4;
        match ptype {
            9 => {
                let id = u16::from_be_bytes([body[0], body[1]]);
                println!("SUBACK: id={id} codes={:?}", &body[2..]);
            }
            3 => {
                let qos = (first >> 1) & 0b11;
                let dup = first & 0b1000 != 0;
                let retain = first & 0b0001 != 0;
                let mut pos = 0;
                let tlen = u16::from_be_bytes([body[0], body[1]]) as usize;
                pos += 2;
                let t = std::str::from_utf8(&body[pos..pos + tlen]).unwrap();
                pos += tlen;
                let (id, payload) = if qos > 0 {
                    let id = u16::from_be_bytes([body[pos], body[pos + 1]]);
                    pos += 2;
                    (Some(id), &body[pos..])
                } else {
                    (None, &body[pos..])
                };
                println!(
                    "PUBLISH: topic={t} qos={qos} dup={dup} retain={retain} id={id:?} payload={:?}",
                    std::str::from_utf8(payload).unwrap_or("<binary>")
                );
                if let Some(id) = id {
                    stream
                        .write_all(&mqtt_subset::codec::encode_puback(id))
                        .unwrap();
                    println!("  -> PUBACK {id} sent");
                }
            }
            13 => println!("PINGRESP"),
            other => println!("unhandled packet type {other}"),
        }
    }
}

/// 读取一个完整 MQTT 帧，返回 (首字节, 报文体)。
fn read_frame(stream: &mut TcpStream) -> std::io::Result<(u8, Vec<u8>)> {
    let first = read_u8(stream)?;
    let mut multiplier = 1usize;
    let mut remaining = 0usize;
    for _ in 0..4 {
        let b = read_u8(stream)?;
        remaining += (b as usize & 0x7F) * multiplier;
        if b & 0x80 == 0 {
            let body = read_exact_n(stream, remaining)?;
            return Ok((first, body));
        }
        multiplier *= 128;
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        "remaining length too long",
    ))
}

fn read_u8(stream: &mut TcpStream) -> std::io::Result<u8> {
    let mut b = [0u8; 1];
    stream.read_exact(&mut b)?;
    Ok(b[0])
}

fn read_exact_n(stream: &mut TcpStream, n: usize) -> std::io::Result<Vec<u8>> {
    let mut out = vec![0u8; n];
    stream.read_exact(&mut out)?;
    Ok(out)
}
