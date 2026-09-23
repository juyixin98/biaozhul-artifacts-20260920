//! 验收场景 3：包标识符复用。
//!
//! 两侧都要验证：
//! - **入站侧**：客户端在事务 A 完成后，把同一个包标识符用于**不同内容**的新
//!   QoS1 PUBLISH，broker 必须把它视为新消息（转发 + PUBACK），而不是误判为重传。
//! - **出站侧**：订阅者确认旧消息后，broker 必须能复用释放出来的包标识符。

mod common;

use common::*;
use std::io::Write;

#[test]
fn inbound_packet_id_reuse_with_new_content_is_new_message() {
    let broker = start_test_broker(300);

    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-reuse-in", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("reuse/in", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-reuse-in", true);
    assert_eq!(rc, 0);

    // 事务 A：id=42，内容 "msg-A"，正常完成。
    pubc.write_all(&publish_frame(
        "reuse/in",
        Some(42),
        b"msg-A",
        1,
        false,
        false,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 42);
    let f1 = read_frame(&mut sub);
    let (_, sid_a, payload, _, _, _) = parse_publish_frame(&f1);
    assert_eq!(payload, b"msg-A");
    sub.write_all(&puback_frame(sid_a.unwrap())).unwrap();

    // 事务 B：客户端复用 id=42 发送不同内容 "msg-B" —— 必须当作新消息。
    pubc.write_all(&publish_frame(
        "reuse/in",
        Some(42),
        b"msg-B",
        1,
        false,
        false,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 42);
    let f2 = read_frame(&mut sub);
    let (_, sid_b, payload, _, dup, _) = parse_publish_frame(&f2);
    assert_eq!(
        payload, b"msg-B",
        "new content on reused client id must be delivered"
    );
    assert!(!dup);
    sub.write_all(&puback_frame(sid_b.unwrap())).unwrap();

    // 去重抑制计数必须为 0（两次都是新消息）。
    assert_eq!(
        broker
            .stats()
            .duplicate_publishes_suppressed
            .load(std::sync::atomic::Ordering::Relaxed),
        0
    );
    broker.shutdown();
}

#[test]
fn outbound_packet_ids_are_reused_after_puback() {
    let broker = start_test_broker(1_000);

    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-reuse-out", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("reuse/out", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-reuse-out", true);
    assert_eq!(rc, 0);

    // 第一条：出站 id 从 1 开始分配；确认后立即释放。
    pubc.write_all(&publish_frame(
        "reuse/out",
        Some(1),
        b"one",
        1,
        false,
        false,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);
    let f1 = read_frame(&mut sub);
    let (_, broker_id_1, payload, _, _, _) = parse_publish_frame(&f1);
    assert_eq!(payload, b"one");
    assert_eq!(broker_id_1, Some(1));
    sub.write_all(&puback_frame(1)).unwrap();

    // 给 broker 处理 PUBACK 的时间。
    sleep_ms(150);

    // 第二条：id=1 已释放，broker 应当复用它（游标从 2 开始但会扫到空闲的 1）。
    pubc.write_all(&publish_frame(
        "reuse/out",
        Some(2),
        b"two",
        1,
        false,
        false,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 2);
    let f2 = read_frame(&mut sub);
    let (_, broker_id_2, payload, _, _, _) = parse_publish_frame(&f2);
    assert_eq!(payload, b"two");
    assert_eq!(
        broker_id_2,
        Some(1),
        "broker must reuse packet identifier 1 after it was acknowledged"
    );
    sub.write_all(&puback_frame(1)).unwrap();

    broker.shutdown();
}
