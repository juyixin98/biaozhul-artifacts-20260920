//! 验收场景 2：重复 PUBLISH（客户端重传）被入站去重抑制，但每次都收到 PUBACK。
//!
//! 场景：PUBACK 从 broker 返回客户端的路径上「丢失」时，真实客户端会以 DUP=1
//! 重发同一 QoS1 PUBLISH。broker 必须：
//! 1) 两次都回 PUBACK（客户端需要它来结束重传）；
//! 2) 只向订阅者转发一次（broker 计数器 duplicate_publishes_suppressed == 1）。

mod common;

use common::*;
use std::io::Write;

#[test]
fn duplicate_inbound_publish_is_acked_once_forwarded_once() {
    let broker = start_test_broker(300);

    let mut sub = raw_connect(&broker);
    let (_, rc) = send_connect(&mut sub, "sub-dup", true);
    assert_eq!(rc, 0);
    sub.write_all(&subscribe_frame(1, &[("dup/x", 1)])).unwrap();
    let _ = read_frame(&mut sub); // SUBACK

    let mut pubc = raw_connect(&broker);
    let (_, rc) = send_connect(&mut pubc, "pub-dup", true);
    assert_eq!(rc, 0);

    let frame = publish_frame("dup/x", Some(77), b"same-body", 1, false, false);
    pubc.write_all(&frame).unwrap();
    // 第一次 PUBACK。
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 77);

    // 订阅者收到唯一一份转发，并立即 PUBACK（否则 QoS1 出站重发会产生第二份，
    // 那属于「至少一次」的出站重发，不是这里要验证的入站重复）。
    let f = read_frame(&mut sub);
    let (_, sid, payload, _, dup, _) = parse_publish_frame(&f);
    assert_eq!(payload, b"same-body");
    assert!(!dup);
    sub.write_all(&puback_frame(sid.unwrap())).unwrap();

    // 客户端原样重发（DUP=1 或 DUP=0 不影响判定；这里按规范置 DUP=1）。
    let retransmit = publish_frame("dup/x", Some(77), b"same-body", 1, true, false);
    pubc.write_all(&retransmit).unwrap();
    // 第二次 PUBACK 必须仍然返回（幂等应答）。
    assert_eq!(parse_puback(&read_frame(&mut pubc)), 77);

    sleep_ms(300);
    assert!(
        try_read_frame(&mut sub).is_none(),
        "duplicate inbound PUBLISH must not be forwarded twice"
    );
    assert_eq!(
        broker
            .stats()
            .duplicate_publishes_suppressed
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );

    broker.shutdown();
}
