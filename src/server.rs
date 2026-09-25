//! 本地 TCP 测试服务器：每连接一个读线程（当前线程）+ 一个写线程。

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;

use crate::broker::Broker;
use crate::codec::{encode, Decoder};
use crate::error::MqttError;
use crate::packet::{ConnAck, Packet, SubAck};

/// 绑定地址并后台接受连接，返回实际监听地址（端口 0 时取系统分配端口）。
pub fn run_listener(
    addr: &str,
    broker: Arc<Broker>,
    max_packet_size: usize,
) -> std::io::Result<SocketAddr> {
    let listener = TcpListener::bind(addr)?;
    let local = listener.local_addr()?;
    thread::spawn(move || {
        for stream in listener.incoming() {
            match stream {
                Ok(s) => {
                    let broker = Arc::clone(&broker);
                    thread::spawn(move || {
                        let _ = handle_conn(s, broker, max_packet_size);
                    });
                }
                Err(_) => break,
            }
        }
    });
    Ok(local)
}

/// dispatch 的结果：继续，或对端请求正常关闭（DISCONNECT）。
enum Step {
    Continue,
    Close,
}

/// 单连接状态机：首包必须 CONNECT，之后进入主循环。
/// 解析/协议错误即关闭连接（MQTT 3.1.1 对畸形报文的规定的处理方式）。
fn handle_conn(
    stream: TcpStream,
    broker: Arc<Broker>,
    max_packet_size: usize,
) -> Result<(), MqttError> {
    stream.set_nodelay(true).ok();
    let mut reader = stream.try_clone()?;
    let mut writer = stream;

    // 写线程：broker 推送到本连接的报文经 channel 汇出。
    let (tx, rx) = mpsc::channel::<Packet>();
    let writer_handle = thread::spawn(move || {
        for packet in rx {
            if writer.write_all(&encode(&packet)).is_err() {
                break;
            }
        }
    });

    let mut decoder = Decoder::new(max_packet_size);
    let mut buf = [0u8; 8192];
    let mut client_id: Option<String> = None;
    let mut graceful = false;
    let mut status: Result<(), MqttError> = Ok(());

    'read: loop {
        let n = match reader.read(&mut buf) {
            Ok(0) => break 'read, // 对端关闭
            Ok(n) => n,
            Err(e) => {
                status = Err(MqttError::Io(e));
                break 'read;
            }
        };
        decoder.feed(&buf[..n]);
        loop {
            match decoder.next_packet() {
                Ok(Some(packet)) => {
                    match dispatch(packet, &broker, &tx, &mut client_id, &mut graceful) {
                        Ok(Step::Continue) => {}
                        Ok(Step::Close) => break 'read,
                        Err(e) => {
                            status = Err(e);
                            break 'read;
                        }
                    }
                }
                Ok(None) => break,
                Err(e) => {
                    status = Err(e);
                    break 'read;
                }
            }
        }
    }

    // 连接收尾：通知 broker 下线（非 DISCONNECT 退出会触发遗嘱），等写线程排空。
    if let Some(id) = &client_id {
        broker.disconnect(id, graceful);
    }
    drop(tx);
    let _ = writer_handle.join();
    status
}

fn send(tx: &mpsc::Sender<Packet>, p: Packet) -> Result<(), MqttError> {
    tx.send(p)
        .map_err(|_| MqttError::ProtocolViolation("connection writer gone"))
}

fn dispatch(
    packet: Packet,
    broker: &Broker,
    tx: &mpsc::Sender<Packet>,
    client_id: &mut Option<String>,
    graceful: &mut bool,
) -> Result<Step, MqttError> {
    match (client_id.is_some(), packet) {
        (false, Packet::Connect(c)) => {
            let mut id = c.client_id.clone();
            if id.is_empty() {
                if !c.clean_session {
                    // MQTT 3.1.1：空 ID + clean=false 必须拒绝。
                    let _ = send(
                        tx,
                        Packet::ConnAck(ConnAck {
                            session_present: false,
                            return_code: 0x02,
                        }),
                    );
                    return Err(MqttError::ProtocolViolation(
                        "empty client id requires clean session",
                    ));
                }
                id = broker.assign_client_id();
            }
            let outcome = broker.connect(&id, c.clean_session, c.will, tx.clone());
            send(
                tx,
                Packet::ConnAck(ConnAck {
                    session_present: outcome.session_present,
                    return_code: 0,
                }),
            )?;
            for p in outcome.resume {
                send(tx, p)?;
            }
            *client_id = Some(id);
            Ok(Step::Continue)
        }
        (false, _) => Err(MqttError::ProtocolViolation("first packet must be CONNECT")),
        (true, Packet::Connect(_)) => Err(MqttError::ProtocolViolation("duplicate CONNECT")),
        (true, Packet::Publish(p)) => {
            let id = client_id.as_ref().unwrap();
            // 重复包（PUBACK 丢失后的重发）不再投递，但仍需回 PUBACK。
            broker.publish_from_client(id, p.clone());
            if p.qos == 1 {
                send(tx, Packet::PubAck(p.packet_id.unwrap()))?;
            }
            Ok(Step::Continue)
        }
        (true, Packet::Subscribe(s)) => {
            let id = client_id.as_ref().unwrap();
            let (granted, retained) = broker.subscribe(id, &s.topics);
            send(
                tx,
                Packet::SubAck(SubAck {
                    packet_id: s.packet_id,
                    granted,
                }),
            )?;
            for p in retained {
                send(tx, p)?;
            }
            Ok(Step::Continue)
        }
        (true, Packet::PubAck(pid)) => {
            broker.puback(client_id.as_ref().unwrap(), pid);
            Ok(Step::Continue)
        }
        (true, Packet::PingReq) => {
            send(tx, Packet::PingResp)?;
            Ok(Step::Continue)
        }
        (true, Packet::Disconnect) => {
            *graceful = true;
            Ok(Step::Close)
        }
        (true, _) => Err(MqttError::ProtocolViolation(
            "unexpected packet from client",
        )),
    }
}
