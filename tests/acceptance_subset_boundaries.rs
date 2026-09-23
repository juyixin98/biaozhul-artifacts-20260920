//! 子集边界（服务端行为）：CONNACK 拒绝码、QoS2 拒绝、保活、PING、长度上限。

mod common;

use common::*;
use mqtt_subset::broker::{Broker, BrokerConfig};
use std::io::{Read, Write};
use std::time::Duration;

#[test]
fn will_connect_is_rejected_with_server_unavailable_0x03() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    s.write_all(&connect_frame("willy", true, 60, 4, true))
        .unwrap();
    let (sp, rc) = read_connack(&mut s);
    assert_eq!(
        (sp, rc),
        (false, 0x03),
        "Will is outside the subset -> CONNACK 0x03"
    );
    // 拒绝后连接被关闭。
    let mut buf = [0u8; 8];
    assert!(matches!(s.read(&mut buf), Ok(0)));
    broker.shutdown();
}

#[test]
fn mqtt31_level_rejected_with_unacceptable_version_0x01() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    // MQTT 3.1 的协议名是 "MQIsdp"；这里直接发级别 3 + MQTT，触发级别拒绝。
    s.write_all(&connect_frame("oldclient", true, 60, 3, false))
        .unwrap();
    let (_, rc) = read_connack(&mut s);
    assert_eq!(rc, 0x01);
    broker.shutdown();
}

#[test]
fn empty_client_id_without_clean_session_rejected_0x02() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    s.write_all(&connect_frame("", false, 60, 4, false))
        .unwrap();
    let (_, rc) = read_connack(&mut s);
    assert_eq!(rc, 0x02);
    broker.shutdown();
}

#[test]
fn empty_client_id_with_clean_session_accepted_as_anonymous() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    s.write_all(&connect_frame("", true, 60, 4, false)).unwrap();
    let (_, rc) = read_connack(&mut s);
    assert_eq!(rc, 0);
    s.write_all(&pingreq_frame()).unwrap();
    let f = read_frame(&mut s);
    assert_eq!(f.first, 0xD0, "expected PINGRESP");
    broker.shutdown();
}

#[test]
fn qos2_publish_closes_connection() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    let (_, rc) = send_connect(&mut s, "qos2-user", true);
    assert_eq!(rc, 0);
    // QoS2 PUBLISH：固定头 = 0x34（QoS=2）。
    s.write_all(&publish_frame("qos2/x", Some(1), b"x", 2, false, false))
        .unwrap();
    sleep_ms(150);
    // 连接应被关闭。
    let mut buf = [0u8; 8];
    assert!(
        matches!(s.read(&mut buf), Ok(0)),
        "QoS2 must cause disconnect"
    );
    assert_eq!(
        broker
            .stats()
            .qos2_rejected
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );
    broker.shutdown();
}

#[test]
fn oversized_packet_is_disconnected() {
    let broker = Broker::start(BrokerConfig {
        bind_addr: "127.0.0.1:0".to_string(),
        max_packet_size: 64,
        retry_interval: Duration::from_secs(1),
        keepalive_multiplier: 1.5,
        quiet: true,
    })
    .unwrap();
    let mut s = raw_connect(&broker);
    let (_, rc) = send_connect(&mut s, "oversize", true);
    assert_eq!(rc, 0);
    // remaining length = 100 (> 64)。
    let mut big = vec![0x30, 100];
    big.extend(std::iter::repeat_n(0u8, 100));
    s.write_all(&big).unwrap();
    sleep_ms(150);
    let mut buf = [0u8; 8];
    assert!(
        matches!(s.read(&mut buf), Ok(0)),
        "oversized packet must close connection"
    );
    broker.shutdown();
}

#[test]
fn ping_pong_keeps_session_alive() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    // keepalive=1：1.5 秒内无活动即断开；期间用 PINGREQ 保活。
    s.write_all(&connect_frame("ping-user", true, 1, 4, false))
        .unwrap();
    let (_, rc) = read_connack(&mut s);
    assert_eq!(rc, 0);
    for _ in 0..4 {
        std::thread::sleep(Duration::from_millis(400));
        s.write_all(&pingreq_frame()).unwrap();
        let f = read_frame(&mut s);
        assert_eq!(f.first, 0xD0);
    }
    // 连接仍然存活。
    s.write_all(&disconnect_frame()).unwrap();
    broker.shutdown();
}

#[test]
fn non_connect_first_packet_is_closed() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    s.write_all(&pingreq_frame()).unwrap(); // 首帧不是 CONNECT
    sleep_ms(150);
    let mut buf = [0u8; 8];
    assert!(matches!(s.read(&mut buf), Ok(0)));
    broker.shutdown();
}

#[test]
fn live_qos_downgraded_to_subscription_qos() {
    let broker = start_test_broker(500);
    // 订阅者以 QoS0 订阅。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-dg", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("dg/x", 0)])).unwrap();
    let _ = read_frame(&mut sub);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-dg", true);
    assert_eq!(rc, 0);
    pubc.write_all(&publish_frame("dg/x", Some(1), b"down", 1, false, false))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);

    let f = read_frame(&mut sub);
    let (_, id, _, qos, _, retain) = parse_publish_frame(&f);
    assert_eq!(
        (qos, id, retain),
        (0, None, false),
        "effective QoS = min(1,0) = 0"
    );
    broker.shutdown();
}

#[test]
fn unknown_puback_is_counted_but_not_fatal() {
    let broker = start_test_broker(500);
    let mut s = raw_connect(&broker);
    let (_, rc) = send_connect(&mut s, "puback-unknown", true);
    assert_eq!(rc, 0);
    s.write_all(&puback_frame(4242)).unwrap();
    // 未知 PUBACK 不关闭连接，PINGRESP 仍能返回。
    sleep_ms(100);
    s.write_all(&pingreq_frame()).unwrap();
    let f = read_frame(&mut s);
    assert_eq!(f.first, 0xD0);
    assert_eq!(
        broker
            .stats()
            .puback_unknown
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );
    broker.shutdown();
}
