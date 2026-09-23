//! 验收场景 1：PUBACK 丢失导致的 QoS1 重发（至少一次语义）。
//!
//! 流程：订阅者 S 以 QoS1 订阅 `chat/1`；发布者 P 发 QoS1 PUBLISH（broker 给 P 回 PUBACK）；
//! S 收到消息但**故意不回 PUBACK**；等待超过重发间隔后，broker 必须以相同包标识符、
//! DUP=1 重发同一消息。S 最后补 PUBACK，broker 停止重发。

mod common;

use common::*;
use std::io::Write;

#[test]
fn puback_loss_triggers_dup_redelivery() {
    let broker = start_test_broker(300);
    let mut sub = raw_connect(&broker);
    let (sp, rc) = send_connect(&mut sub, "sub-puback-loss", false);
    assert_eq!((sp, rc), (false, 0));
    sub.write_all(&subscribe_frame(10, &[("chat/1", 1)]))
        .unwrap();
    let suback = read_frame(&mut sub);
    assert_eq!(parse_suback(&suback), (10, vec![1]));

    let mut publisher = raw_connect(&broker);
    let (_, rc) = send_connect(&mut publisher, "pub-puback-loss", true);
    assert_eq!(rc, 0);
    publisher
        .write_all(&publish_frame(
            "chat/1",
            Some(500),
            b"first",
            1,
            false,
            false,
        ))
        .unwrap();
    let p_ack = read_frame(&mut publisher);
    assert_eq!(parse_puback(&p_ack), 500, "publisher must be PUBACKed");

    // 订阅者首次收到：broker 分配的出站包标识符从 1 开始，DUP=0。
    let f1 = read_frame(&mut sub);
    let (topic, id1, payload, qos, dup, retain) = parse_publish_frame(&f1);
    assert_eq!(
        (topic.as_str(), qos, dup, retain, payload.as_slice()),
        ("chat/1", 1, false, false, b"first".as_slice())
    );

    // 故意不回 PUBACK：等待重发（间隔 300ms，留足调度余量）。
    sleep_ms(700);
    let f2 = read_frame(&mut sub);
    let (topic2, id2, payload2, qos2, dup2, _) = parse_publish_frame(&f2);
    assert_eq!(topic2, "chat/1");
    assert_eq!(qos2, 1);
    assert_eq!(id2, id1, "redelivery keeps the same packet identifier");
    assert!(dup2, "redelivery MUST set DUP=1 (MQTT 3.1.1 §3.3.1.1)");
    assert_eq!(payload2, b"first".as_slice());

    // 此时 broker 的在途集合仍持有该消息（尚未确认）。
    // 补上 PUBACK 后，等待一个重发周期以上，不应再有第三份。
    sub.write_all(&puback_frame(id1.unwrap())).unwrap();
    sleep_ms(700);
    assert!(
        try_read_frame(&mut sub).is_none(),
        "after PUBACK, no further redelivery is allowed"
    );

    drop(publisher);
    broker.shutdown();
}
