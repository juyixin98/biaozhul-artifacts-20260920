//! 验收场景 4/5：保留消息（Retained Messages）与订阅匹配（含通配符）。
//!
//! 覆盖 MQTT 3.1.1 §3.3.1.3 与 §4.7：
//! - RETAIN=1 的 PUBLISH 在无订阅者时也会被存储；
//! - 新订阅建立后立即收到匹配的保留消息，且该投递 RETAIN=1；
//! - 后续实时消息 RETAIN=0；
//! - RETAIN=1 空载荷删除保留消息；
//! - `+` / `#` 通配符按级匹配；
//! - 同一主题保留消息被新值覆盖。

mod common;

use common::*;
use std::io::Write;

#[test]
fn retained_message_delivered_to_later_subscriber_with_retain_flag() {
    let broker = start_test_broker(500);

    // 先发布保留消息（此时没有任何订阅者）。
    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-retained", true);
    assert_eq!(rc, 0);
    pubc.write_all(&publish_frame(
        "sensors/room1/temp",
        Some(1),
        b"21.5",
        1,
        false,
        true,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);
    drop(pubc);
    sleep_ms(100);

    // 之后新订阅者精确订阅：SUBACK 后应收到保留消息，RETAIN=1。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-retained", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(9, &[("sensors/room1/temp", 1)]))
        .unwrap();
    let ack = read_frame(&mut sub);
    assert_eq!(parse_suback(&ack), (9, vec![1]));

    let f = read_frame(&mut sub);
    let (topic, _id, payload, qos, dup, retain) = parse_publish_frame(&f);
    assert_eq!(topic, "sensors/room1/temp");
    assert_eq!(payload, b"21.5");
    assert_eq!(
        (qos, dup, retain),
        (1, false, true),
        "retained delivery must carry RETAIN=1"
    );

    // 再来一条普通实时消息：RETAIN=0。
    let mut pubc2 = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc2, "pub-retained-2", true);
    assert_eq!(rc, 0);
    pubc2
        .write_all(&publish_frame(
            "sensors/room1/temp",
            Some(2),
            b"22.0",
            1,
            false,
            true,
        ))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc2)), 2);

    let f2 = read_frame(&mut sub);
    let (_, _, payload2, _, _, retain2) = parse_publish_frame(&f2);
    assert_eq!(payload2, b"22.0");
    // 注意：这条消息发布时也带 RETAIN=1（更新了存储），但对在线订阅者的实时
    // 投递必须 RETAIN=0（§3.3.1.3：RETAIN 位仅对「作为保留消息的投递」置 1）。
    assert!(
        !retain2,
        "live delivery of a RETAIN publish keeps RETAIN=0 for existing subscribers"
    );

    broker.shutdown();
}

#[test]
fn wildcard_subscriptions_match_and_retained_followed_by_hash() {
    let broker = start_test_broker(500);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-wc", true);
    assert_eq!(rc, 0);
    // 预置三条不同层级的保留消息。
    for (i, (topic, val)) in [
        ("wc/a", b"1".as_slice()),
        ("wc/a/b", b"2"),
        ("wc/other/c", b"3"),
    ]
    .iter()
    .enumerate()
    {
        pubc.write_all(&publish_frame(
            topic,
            Some(10 + i as u16),
            val,
            1,
            false,
            true,
        ))
        .unwrap();
        assert_eq!(parse_puback(&read_frame(&mut pubc)), 10 + i as u16);
    }
    drop(pubc);
    sleep_ms(100);

    // 订阅 wc/+ ：只匹配恰好两级的 wc/a。
    let mut sub1 = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub1, "sub-plus", true);
    assert_eq!(rc, 0);
    sub1.write_all(&subscribe_frame(1, &[("wc/+", 1)])).unwrap();
    let _ = read_frame(&mut sub1);
    let f = read_frame(&mut sub1);
    let (topic, rid, payload, _, _, _) = parse_publish_frame(&f);
    assert_eq!(
        (topic.as_str(), payload.as_slice()),
        ("wc/a", b"1".as_slice())
    );
    sub1.write_all(&puback_frame(rid.unwrap())).unwrap();
    assert!(
        try_read_frame(&mut sub1).is_none(),
        "wc/+ must not match wc/a/b or wc/other/c"
    );

    // 订阅 wc/# ：匹配 wc 下全部层级，共 3 条。
    let mut sub2 = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub2, "sub-hash", true);
    assert_eq!(rc, 0);
    sub2.write_all(&subscribe_frame(1, &[("wc/#", 1)])).unwrap();
    let _ = read_frame(&mut sub2);
    let mut got = Vec::new();
    for _ in 0..3 {
        let f = read_frame(&mut sub2);
        let (t, id, p, _, _, ret) = parse_publish_frame(&f);
        assert!(ret);
        got.push((t, String::from_utf8(p).unwrap()));
        // 立即确认，避免 QoS1 在途重发干扰后续断言。
        sub2.write_all(&puback_frame(id.unwrap())).unwrap();
    }
    got.sort();
    assert_eq!(
        got,
        vec![
            ("wc/a".to_string(), "1".to_string()),
            ("wc/a/b".to_string(), "2".to_string()),
            ("wc/other/c".to_string(), "3".to_string()),
        ]
    );
    assert!(try_read_frame(&mut sub2).is_none());

    // 实时路由：wc/a/x 上的 + 不匹配（三级），# 匹配。
    let mut pubc2 = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc2, "pub-wc2", true);
    assert_eq!(rc, 0);
    pubc2
        .write_all(&publish_frame("wc/a/x", None, b"live", 0, false, false))
        .unwrap();

    assert!(
        try_read_frame(&mut sub1).is_none(),
        "wc/+ must not receive wc/a/x"
    );
    let f = read_frame(&mut sub2);
    let (t, _, p, qos, _, ret) = parse_publish_frame(&f);
    assert_eq!(
        (t.as_str(), p.as_slice(), qos, ret),
        ("wc/a/x", b"live".as_slice(), 0, false)
    );

    broker.shutdown();
}

#[test]
fn retained_message_overwritten_and_cleared_by_empty_payload() {
    let broker = start_test_broker(500);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-clear", true);
    assert_eq!(rc, 0);

    // v1
    pubc.write_all(&publish_frame("ret/clear", Some(1), b"v1", 1, false, true))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);
    // v2 覆盖
    pubc.write_all(&publish_frame(
        "ret/clear",
        Some(2),
        b"v2-newer",
        1,
        false,
        true,
    ))
    .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 2);
    // RETAIN=1 + 空载荷：删除
    pubc.write_all(&publish_frame("ret/clear", Some(3), b"", 1, false, true))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 3);
    drop(pubc);
    sleep_ms(100);

    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-clear", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("ret/clear", 1)]))
        .unwrap();
    let _ = read_frame(&mut sub);
    assert!(
        try_read_frame(&mut sub).is_none(),
        "deleted retained message must not be delivered"
    );

    broker.shutdown();
}

#[test]
fn qos0_retained_downgraded_on_qos0_subscription() {
    let broker = start_test_broker(500);

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-q0ret", true);
    assert_eq!(rc, 0);
    // QoS1 保留消息；
    pubc.write_all(&publish_frame("dq/x", Some(1), b"q", 1, false, true))
        .unwrap();
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 1);
    drop(pubc);
    sleep_ms(100);

    // 以 QoS0 订阅：保留消息按 min(1,0)=QoS0 投递（无包ID）。
    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-q0", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("dq/x", 0)])).unwrap();
    let ack = read_frame(&mut sub);
    assert_eq!(parse_suback(&ack), (1, vec![0]));
    let f = read_frame(&mut sub);
    let (_, id, payload, qos, _, retain) = parse_publish_frame(&f);
    assert_eq!(payload, b"q");
    assert_eq!((qos, id, retain), (0, None, true));

    broker.shutdown();
}
