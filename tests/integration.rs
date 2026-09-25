//! 端到端集成测试：真实 TCP 连接 + 手写测试客户端（同样只用本库的 codec）。
//!
//! 覆盖验收项：
//! - PUBACK 丢失 → 重连后 DUP=1 重发
//! - 重复 PUBLISH（入向去重）
//! - 包 ID 复用（PUBACK 完成后同 ID 再发）
//! - 会话重连（clean=false 恢复订阅与在飞消息；clean=true 清空）
//! - 保留消息与订阅匹配

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::Arc;
use std::time::{Duration, Instant};

use mqtt_subset::broker::Broker;
use mqtt_subset::codec::{encode, Decoder};
use mqtt_subset::packet::{ConnAck, Connect, Packet, Publish, Subscribe};
use mqtt_subset::server::run_listener;
use mqtt_subset::DEFAULT_MAX_PACKET_SIZE;

const MAX_PACKET: usize = 1024; // 测试用小上限，便于触发超限

fn start_server() -> (SocketAddr, Arc<Broker>) {
    let broker = Arc::new(Broker::new());
    let addr = run_listener("127.0.0.1:0", Arc::clone(&broker), MAX_PACKET).unwrap();
    (addr, broker)
}

/// 同步测试客户端。
struct TestClient {
    stream: TcpStream,
    dec: Decoder,
}

impl TestClient {
    fn new(addr: SocketAddr) -> Self {
        let stream = TcpStream::connect(addr).unwrap();
        stream.set_nodelay(true).unwrap();
        TestClient {
            stream,
            dec: Decoder::new(DEFAULT_MAX_PACKET_SIZE),
        }
    }

    fn connect(addr: SocketAddr, client_id: &str, clean: bool) -> (Self, ConnAck) {
        let mut c = Self::new(addr);
        c.send(&Packet::Connect(Connect {
            client_id: client_id.to_string(),
            clean_session: clean,
            keep_alive: 30,
            will: None,
            username: None,
            password: None,
        }));
        match c.recv() {
            Some(Packet::ConnAck(a)) => (c, a),
            other => panic!("expected CONNACK, got {other:?}"),
        }
    }

    fn send(&mut self, p: &Packet) {
        self.stream.write_all(&encode(p)).unwrap();
    }

    fn send_raw(&mut self, bytes: &[u8]) {
        self.stream.write_all(bytes).unwrap();
    }

    /// 在超时时间内等待一个报文；超时或对端关闭返回 None。
    fn try_recv(&mut self, timeout: Duration) -> Option<Packet> {
        let deadline = Instant::now() + timeout;
        let mut buf = [0u8; 4096];
        loop {
            if let Ok(Some(p)) = self.dec.next_packet() {
                return Some(p);
            }
            let now = Instant::now();
            if now >= deadline {
                return None;
            }
            self.stream
                .set_read_timeout(Some(deadline - now))
                .unwrap();
            match self.stream.read(&mut buf) {
                Ok(0) => return None, // 对端关闭
                Ok(n) => self.dec.feed(&buf[..n]),
                Err(_) => return None, // 超时
            }
        }
    }

    fn recv(&mut self) -> Option<Packet> {
        self.try_recv(Duration::from_secs(3))
    }

    fn recv_publish(&mut self) -> Publish {
        match self.recv() {
            Some(Packet::Publish(p)) => p,
            other => panic!("expected PUBLISH, got {other:?}"),
        }
    }

    fn subscribe(&mut self, packet_id: u16, filter: &str, qos: u8) -> Vec<u8> {
        self.send(&Packet::Subscribe(Subscribe {
            packet_id,
            topics: vec![(filter.to_string(), qos)],
        }));
        match self.recv() {
            Some(Packet::SubAck(a)) => {
                assert_eq!(a.packet_id, packet_id);
                a.granted
            }
            other => panic!("expected SUBACK, got {other:?}"),
        }
    }

    /// 静默期断言：给定时间内不应收到任何报文。
    fn expect_silence(&mut self, dur: Duration) {
        if let Some(p) = self.try_recv(dur) {
            panic!("expected silence, got {p:?}");
        }
    }
}

fn qos1_publish(topic: &str, id: u16, payload: &str) -> Packet {
    let mut p = Publish::new(topic, 1, payload.as_bytes().to_vec());
    p.packet_id = Some(id);
    Packet::Publish(p)
}

// ---------- 验收场景 ----------

#[test]
fn basic_connect_subscribe_publish_qos1() {
    let (addr, _broker) = start_server();
    let (mut a, ack) = TestClient::connect(addr, "sub-a", true);
    assert!(!ack.session_present);
    assert_eq!(a.subscribe(1, "sensors/+", 1), vec![1]);

    let (mut b, _) = TestClient::connect(addr, "pub-b", true);
    b.send(&qos1_publish("sensors/temp", 10, "21.5"));
    assert_eq!(b.recv(), Some(Packet::PubAck(10)));

    let got = a.recv_publish();
    assert_eq!(got.topic, "sensors/temp");
    assert_eq!(got.qos, 1);
    assert_eq!(got.payload, b"21.5");
    assert!(!got.dup && !got.retain);
    let server_id = got.packet_id.expect("qos1 needs packet id");
    a.send(&Packet::PubAck(server_id));

    // PING
    b.send(&Packet::PingReq);
    assert_eq!(b.recv(), Some(Packet::PingResp));
}

#[test]
fn puback_loss_triggers_dup_retransmit_on_reconnect() {
    let (addr, broker) = start_server();
    // 订阅方使用持久会话
    let (mut a, _) = TestClient::connect(addr, "sub-persist", false);
    a.subscribe(1, "t/#", 1);

    let (mut b, _) = TestClient::connect(addr, "pub-c", true);
    b.send(&qos1_publish("t/x", 1, "m1"));
    assert_eq!(b.recv(), Some(Packet::PubAck(1)));

    // A 收到 QoS1 PUBLISH 但模拟 PUBACK 丢失：不回 PUBACK，直接断开
    let first = a.recv_publish();
    assert!(!first.dup);
    let pid = first.packet_id.unwrap();
    drop(a);
    // 等服务器感知断连
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(broker.inflight_count("sub-persist"), 1);

    // 重连：session_present=1，同一包 ID 以 DUP=1 重发
    let (mut a2, ack2) = TestClient::connect(addr, "sub-persist", false);
    assert!(ack2.session_present);
    let resent = a2.recv_publish();
    assert!(resent.dup, "retransmit must set DUP");
    assert_eq!(resent.packet_id, Some(pid));
    assert_eq!(resent.payload, b"m1");
    a2.send(&Packet::PubAck(pid));
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(broker.inflight_count("sub-persist"), 0);

    // 再次重连：已确认，不应再重发
    let (mut a3, ack3) = TestClient::connect(addr, "sub-persist", false);
    assert!(ack3.session_present);
    a3.expect_silence(Duration::from_millis(300));
}

#[test]
fn duplicate_publish_is_deduplicated() {
    let (addr, _broker) = start_server();
    let (mut a, _) = TestClient::connect(addr, "sub-d", true);
    a.subscribe(1, "d/#", 1);

    // B 用持久会话发 QoS1，随后模拟“PUBACK 丢失”：不读直接断开
    let (mut b, _) = TestClient::connect(addr, "pub-d", false);
    b.send(&qos1_publish("d/x", 7, "payload-7"));
    let first = a.recv_publish();
    assert_eq!(first.payload, b"payload-7");
    a.send(&Packet::PubAck(first.packet_id.unwrap()));
    drop(b); // PUBACK(7) 视为丢失
    std::thread::sleep(Duration::from_millis(200));

    // B 重连并以 DUP=1 重发同一包 ID：服务器应再次 PUBACK，但不再投递
    let (mut b2, ack) = TestClient::connect(addr, "pub-d", false);
    assert!(ack.session_present);
    let mut resend = Publish::new("d/x", 1, b"payload-7".to_vec());
    resend.dup = true;
    resend.packet_id = Some(7);
    b2.send(&Packet::Publish(resend));
    assert_eq!(b2.recv(), Some(Packet::PubAck(7)));
    a.expect_silence(Duration::from_millis(300));
}

#[test]
fn packet_id_reuse_after_puback() {
    let (addr, _broker) = start_server();
    let (mut a, _) = TestClient::connect(addr, "sub-r", true);
    a.subscribe(1, "r/#", 1);

    let (mut b, _) = TestClient::connect(addr, "pub-r", true);
    // 第一次使用包 ID 9
    b.send(&qos1_publish("r/1", 9, "first"));
    assert_eq!(b.recv(), Some(Packet::PubAck(9)));
    let m1 = a.recv_publish();
    a.send(&Packet::PubAck(m1.packet_id.unwrap()));

    // PUBACK 完成后复用同一包 ID：必须作为新消息正常处理
    b.send(&qos1_publish("r/2", 9, "second"));
    assert_eq!(b.recv(), Some(Packet::PubAck(9)));
    let m2 = a.recv_publish();
    assert_eq!(m2.payload, b"second");
    a.send(&Packet::PubAck(m2.packet_id.unwrap()));
}

#[test]
fn session_reconnect_persistent_vs_clean() {
    let (addr, _broker) = start_server();
    // 持久会话：订阅在重连后保留，离线消息补投
    let (mut a, _) = TestClient::connect(addr, "persist", false);
    a.subscribe(1, "p/#", 1);
    a.send(&Packet::Disconnect);
    drop(a);
    std::thread::sleep(Duration::from_millis(200));

    let (mut b, _) = TestClient::connect(addr, "pub-p", true);
    b.send(&qos1_publish("p/offline", 1, "while-away"));
    assert_eq!(b.recv(), Some(Packet::PubAck(1)));

    let (mut a2, ack) = TestClient::connect(addr, "persist", false);
    assert!(ack.session_present);
    let queued = a2.recv_publish();
    assert_eq!(queued.payload, b"while-away");
    assert!(!queued.dup, "离线队列首发不应置 DUP");
    a2.send(&Packet::PubAck(queued.packet_id.unwrap()));

    // clean=true 重连：会话被清空
    let (mut a3, ack3) = TestClient::connect(addr, "persist", true);
    assert!(!ack3.session_present);
    b.send(&qos1_publish("p/after-clean", 2, "gone"));
    assert_eq!(b.recv(), Some(Packet::PubAck(2)));
    a3.expect_silence(Duration::from_millis(300));
}

#[test]
fn retained_message_and_filter_matching() {
    let (addr, broker) = start_server();
    let (mut b, _) = TestClient::connect(addr, "pub-ret", true);
    let mut retained = Publish::new("sensors/temp", 1, b"21.5".to_vec());
    retained.retain = true;
    retained.packet_id = Some(1);
    b.send(&Packet::Publish(retained));
    assert_eq!(b.recv(), Some(Packet::PubAck(1)));
    // 不匹配的主题也放一条保留消息（QoS1 + PUBACK 确保服务器已处理）
    let mut other = Publish::new("other/x", 1, b"nope".to_vec());
    other.retain = true;
    other.packet_id = Some(9);
    b.send(&Packet::Publish(other));
    assert_eq!(b.recv(), Some(Packet::PubAck(9)));
    assert_eq!(broker.retained_topics(), vec!["other/x", "sensors/temp"]);

    // 新订阅 sensors/+：只应收到匹配的保留消息，retain=1
    let (mut a, _) = TestClient::connect(addr, "sub-ret", true);
    assert_eq!(a.subscribe(1, "sensors/+", 1), vec![1]);
    let got = a.recv_publish();
    assert_eq!(got.topic, "sensors/temp");
    assert!(got.retain, "retained delivery keeps retain flag");
    assert_eq!(got.payload, b"21.5");
    a.send(&Packet::PubAck(got.packet_id.unwrap()));
    a.expect_silence(Duration::from_millis(200));

    // 之后的实时投递 retain=0
    b.send(&qos1_publish("sensors/hum", 2, "40"));
    assert_eq!(b.recv(), Some(Packet::PubAck(2)));
    let live = a.recv_publish();
    assert!(!live.retain);
    a.send(&Packet::PubAck(live.packet_id.unwrap()));

    // 空载荷清除保留消息
    let mut clear = Publish::new("sensors/temp", 1, Vec::new());
    clear.retain = true;
    clear.packet_id = Some(3);
    b.send(&Packet::Publish(clear));
    assert_eq!(b.recv(), Some(Packet::PubAck(3)));
    assert_eq!(broker.retained_topics(), vec!["other/x"]);

    let (mut c, _) = TestClient::connect(addr, "sub-ret2", true);
    c.subscribe(1, "sensors/#", 1);
    c.expect_silence(Duration::from_millis(300));
}

#[test]
fn will_delivered_on_abnormal_disconnect() {
    let (addr, _broker) = start_server();
    let (mut a, _) = TestClient::connect(addr, "watcher", true);
    a.subscribe(1, "status/+", 1);

    let mut b = TestClient::new(addr);
    b.send(&Packet::Connect(Connect {
        client_id: "dev-b".into(),
        clean_session: true,
        keep_alive: 30,
        will: Some(Publish::new("status/dev-b", 1, b"offline".to_vec())),
        username: None,
        password: None,
    }));
    assert!(matches!(b.recv(), Some(Packet::ConnAck(_))));
    drop(b); // 异常断开：未发 DISCONNECT

    let will = a.recv_publish();
    assert_eq!(will.topic, "status/dev-b");
    assert_eq!(will.payload, b"offline");
    a.send(&Packet::PubAck(will.packet_id.unwrap()));
}

#[test]
fn protocol_errors_close_connection() {
    let (addr, _broker) = start_server();

    // 1) 首包不是 CONNECT
    let mut c = TestClient::new(addr);
    c.send(&Packet::PingReq);
    assert_eq!(c.recv(), None, "server must close: first packet not CONNECT");

    // 2) 超过长度上限（声明 5000 字节，上限 1024）
    let mut c = TestClient::new(addr);
    c.send_raw(&[0x30, 0x88, 0x27]); // PUBLISH, remaining=5000
    assert_eq!(c.recv(), None, "server must close: packet too large");

    // 3) QoS2 PUBLISH（子集不支持）
    let (mut c, _) = TestClient::connect(addr, "qos2", true);
    c.send_raw(&[0x34, 0x05, 0x00, 0x01, b'a', 0x00, 0x01]); // qos=2
    assert_eq!(c.recv(), None, "server must close: qos2 unsupported");

    // 4) 重复 CONNECT
    let (mut c, _) = TestClient::connect(addr, "dup-conn", true);
    c.send(&Packet::Connect(Connect {
        client_id: "dup-conn".into(),
        clean_session: true,
        keep_alive: 30,
        will: None,
        username: None,
        password: None,
    }));
    assert_eq!(c.recv(), None, "server must close: duplicate CONNECT");
}

#[test]
fn oversized_packet_limit_is_configurable() {
    // 用默认 64KiB 上限的服务器验证大包可通过
    let broker = Arc::new(Broker::new());
    let addr = run_listener("127.0.0.1:0", broker, DEFAULT_MAX_PACKET_SIZE).unwrap();
    let (mut a, _) = TestClient::connect(addr, "big-sub", true);
    a.subscribe(1, "big/#", 0);
    let (mut b, _) = TestClient::connect(addr, "big-pub", true);
    let payload = vec![b'x'; 32 * 1024];
    let mut p = Publish::new("big/msg", 0, payload.clone());
    p.packet_id = None;
    b.send(&Packet::Publish(p));
    let got = a.recv_publish();
    assert_eq!(got.payload, payload);
}
