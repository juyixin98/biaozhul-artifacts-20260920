//! 命令行样例客户端：演示 CONNECT / SUBSCRIBE / PUBLISH(QoS1) / PUBACK。
//!
//! 用法：
//!   cargo run --example mqtt_client -- sub  --id sub1 --topic 'sensors/+'
//!   cargo run --example mqtt_client -- pub  --id pub1 --topic sensors/temp \
//!         --payload '21.5' --qos 1 --retain

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use mqtt_subset::codec::{encode, Decoder};
use mqtt_subset::packet::{Connect, Packet, Publish, Subscribe};
use mqtt_subset::DEFAULT_MAX_PACKET_SIZE;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let get = |key: &str, default: &str| -> String {
        args.windows(2)
            .find(|w| w[0] == key)
            .map(|w| w[1].clone())
            .unwrap_or_else(|| default.to_string())
    };
    let addr = get("--addr", "127.0.0.1:18830");
    let id = get("--id", "sample-client");
    let topic = get("--topic", "sensors/temp");
    let clean = !args.iter().any(|a| a == "--persistent");

    let mut stream = TcpStream::connect(&addr).expect("connect failed");
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
    let mut dec = Decoder::new(DEFAULT_MAX_PACKET_SIZE);

    let connect = Packet::Connect(Connect {
        client_id: id.clone(),
        clean_session: clean,
        keep_alive: 60,
        will: None,
        username: None,
        password: None,
    });
    stream.write_all(&encode(&connect)).unwrap();
    match recv(&mut stream, &mut dec) {
        Some(Packet::ConnAck(a)) => {
            println!("CONNACK session_present={} return_code={}", a.session_present, a.return_code);
            if a.return_code != 0 {
                std::process::exit(1);
            }
        }
        other => {
            eprintln!("expected CONNACK, got {other:?}");
            std::process::exit(1);
        }
    }

    match args.first().map(String::as_str) {
        Some("sub") => {
            let sub = Packet::Subscribe(Subscribe {
                packet_id: 1,
                topics: vec![(topic.clone(), 1)],
            });
            stream.write_all(&encode(&sub)).unwrap();
            println!("subscribed to {topic}, waiting for messages (Ctrl-C to quit)...");
            loop {
                match recv(&mut stream, &mut dec) {
                    Some(Packet::SubAck(a)) => println!("SUBACK granted={:?}", a.granted),
                    Some(Packet::Publish(p)) => {
                        println!(
                            "PUBLISH topic={} qos={} dup={} retain={} id={:?} payload={:?}",
                            p.topic,
                            p.qos,
                            p.dup,
                            p.retain,
                            p.packet_id,
                            String::from_utf8_lossy(&p.payload)
                        );
                        if p.qos == 1 {
                            stream
                                .write_all(&encode(&Packet::PubAck(p.packet_id.unwrap())))
                                .unwrap();
                        }
                    }
                    Some(other) => println!("{other:?}"),
                    None => {
                        println!("connection closed");
                        break;
                    }
                }
            }
        }
        Some("pub") => {
            let payload = get("--payload", "hello").into_bytes();
            let retain = args.iter().any(|a| a == "--retain");
            let mut p = Publish::new(&topic, 1, payload);
            p.retain = retain;
            p.packet_id = Some(1);
            stream.write_all(&encode(&Packet::Publish(p))).unwrap();
            match recv(&mut stream, &mut dec) {
                Some(Packet::PubAck(id)) => println!("PUBACK id={id}"),
                other => {
                    eprintln!("expected PUBACK, got {other:?}");
                    std::process::exit(1);
                }
            }
            stream.write_all(&encode(&Packet::Disconnect)).unwrap();
            println!("done");
        }
        _ => {
            eprintln!("usage: mqtt_client <sub|pub> [--addr A] [--id ID] [--topic T] [--payload P] [--retain] [--persistent]");
            std::process::exit(2);
        }
    }
}

fn recv(stream: &mut TcpStream, dec: &mut Decoder) -> Option<Packet> {
    let mut buf = [0u8; 4096];
    loop {
        if let Ok(Some(p)) = dec.next_packet() {
            return Some(p);
        }
        match stream.read(&mut buf) {
            Ok(0) => return None,
            Ok(n) => dec.feed(&buf[..n]),
            Err(_) => return None,
        }
    }
}
