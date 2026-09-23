//! 验收场景 6：持久会话重连（CleanSession=0）。
//!
//! - 客户端断线期间发布的 QoS1 消息必须在重连后投递（离线队列，FIFO）；
//! - CONNACK 的 Session Present 标志：首次连接 0，重连 1；
//! - 断线前未确认的在途消息重连后以 DUP=1、相同包标识符重发；
//! - CleanSession=1 的客户端断线后不保留任何状态（对照测试）。

mod common;

use common::*;
use std::io::Write;

#[test]
fn persistent_session_reconnect_gets_offline_qos1_messages() {
    let broker = start_test_broker(500);

    // 1) 持久会话首次连接并订阅，然后直接 drop TCP（模拟掉线，不发 DISCONNECT）。
    let mut sub = raw_connect(&broker);
    let (sp, rc) = send_connect(&mut sub, "persist-1", false);
    assert_eq!((sp, rc), (false, 0));
    sub.write_all(&subscribe_frame(1, &[("persist/data", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);
    drop(sub);
    sleep_ms(300); // 等待 broker 感知 EOF。

    assert_eq!(
        broker.session_count(),
        1,
        "persistent session stays after TCP drop"
    );

    // 2) 掉线期间由另一客户端发两条 QoS1 消息。
    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-offline", true);
    assert_eq!(rc, 0);
    for (i, body) in [b"offline-1".as_slice(), b"offline-2"].iter().enumerate() {
        pubc.write_all(&publish_frame(
            "persist/data",
            Some(100 + i as u16),
            body,
            1,
            false,
            false,
        ))
        .unwrap();
        assert_eq!(parse_puback(&read_frame(&mut pubc)), 100 + i as u16);
    }
    drop(pubc);

    assert_eq!(
        broker.offline_len("persist-1"),
        Some(2),
        "messages for an offline persistent session are queued"
    );

    // 3) 持久客户端重连：CONNACK Session Present=1。
    let mut sub2 = raw_connect(&broker);
    sub2.write_all(&connect_frame("persist-1", false, 60, 4, false))
        .unwrap();
    let (sp, rc) = read_connack(&mut sub2);
    assert_eq!((sp, rc), (true, 0));

    // 不需要重新 SUBSCRIBE：订阅属于会话状态。离线消息按 FIFO 送达。
    let f1 = read_frame(&mut sub2);
    let (_, id1, p1, _, dup1, _) = parse_publish_frame(&f1);
    assert_eq!(p1, b"offline-1");
    assert!(
        !dup1,
        "queued (never-sent) messages are delivered with DUP=0"
    );
    sub2.write_all(&puback_frame(id1.unwrap())).unwrap();

    let f2 = read_frame(&mut sub2);
    let (_, _id2, p2, _, dup2, _) = parse_publish_frame(&f2);
    assert_eq!(p2, b"offline-2");
    assert!(!dup2);

    // 无多余消息。
    assert!(try_read_frame(&mut sub2).is_none());
    broker.shutdown();
}

#[test]
fn unacked_inflight_is_resent_with_dup_on_reconnect() {
    let broker = start_test_broker(500);

    // 订阅者持久连接，收到 QoS1 但不确认，随后掉线。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "persist-inflight", false);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("p/inf", 1)])).unwrap();
    let _ = read_frame(&mut sub);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-inf", true);
    assert_eq!(rc, 0);
    pubc.write_all(&publish_frame(
        "p/inf",
        Some(5),
        b"unacked",
        1,
        false,
        false,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 5);

    let f = read_frame(&mut sub);
    let (_, broker_id, payload, _, _, _) = parse_publish_frame(&f);
    assert_eq!(payload, b"unacked");
    let broker_id = broker_id.unwrap();
    // 不回 PUBACK，直接断 TCP。
    drop(sub);
    sleep_ms(300);

    // 重连：在途消息必须以相同 id、DUP=1 重发，且早于任何新离线消息。
    let mut sub2 = raw_connect(&broker);
    sub2.write_all(&connect_frame("persist-inflight", false, 60, 4, false))
        .unwrap();
    let (sp, rc) = read_connack(&mut sub2);
    assert_eq!((sp, rc), (true, 0));

    let f2 = read_frame(&mut sub2);
    let (_, id2, p2, _, dup2, _) = parse_publish_frame(&f2);
    assert_eq!(p2, b"unacked");
    assert_eq!(id2, Some(broker_id));
    assert!(dup2, "unacked in-flight message is redelivered with DUP=1");
    sub2.write_all(&puback_frame(id2.unwrap())).unwrap();

    // 确认后不再重发。
    sleep_ms(400);
    assert!(try_read_frame(&mut sub2).is_none());
    broker.shutdown();
}

#[test]
fn clean_session_discards_everything_on_reconnect() {
    let broker = start_test_broker(500);

    // CleanSession=1 客户端订阅后「崩溃」（drop TCP）。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "clean-1", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("clean/x", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);
    drop(sub);
    sleep_ms(300);
    assert_eq!(
        broker.session_count(),
        0,
        "clean sessions are deleted on disconnect"
    );

    // 掉线期间的消息无目标可投。
    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-clean", true);
    assert_eq!(rc, 0);
    pubc.write_all(&publish_frame("clean/x", Some(1), b"lost", 1, false, false))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);
    drop(pubc);
    sleep_ms(100);

    // 同名客户端重连：Session Present=0，无订阅 → 收不到旧消息。
    let mut sub2 = raw_connect(&broker);
    sub2.write_all(&connect_frame("clean-1", true, 60, 4, false))
        .unwrap();
    let (sp, rc) = read_connack(&mut sub2);
    assert_eq!((sp, rc), (false, 0));
    assert!(try_read_frame(&mut sub2).is_none());

    broker.shutdown();
}

#[test]
fn clean_reconnect_discards_old_persistent_state() {
    let broker = start_test_broker(500);

    // 先以持久会话建立订阅。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "flip", false);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("flip/x", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);
    // 干净 DISCONNECT：持久会话仍然保留。
    sub.write_all(&disconnect_frame()).unwrap();
    drop(sub);
    sleep_ms(200);
    assert_eq!(broker.session_count(), 1);

    // 以 CleanSession=1 重连：旧状态必须被删除（§3.1.2.4）。
    let mut sub2 = raw_connect(&broker);
    sub2.write_all(&connect_frame("flip", true, 60, 4, false))
        .unwrap();
    let (sp, rc) = read_connack(&mut sub2);
    assert_eq!(
        (sp, rc),
        (false, 0),
        "clean reconnect must not show session present"
    );

    // 此期间无订阅，消息不会送达该连接。
    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-flip", true);
    assert_eq!(rc, 0);
    pubc.write_all(&publish_frame("flip/x", Some(1), b"x", 0, false, false))
        .unwrap();
    drop(pubc);
    assert!(try_read_frame(&mut sub2).is_none());

    broker.shutdown();
}
