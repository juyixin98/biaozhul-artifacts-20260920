//! 增量解析器：半包/粘包、CONNECT 子集字段、错误类型。

mod common;

use mqtt_subset::codec::{
    encode_puback, encode_publish, encode_suback, Decoder, DEFAULT_MAX_PACKET_SIZE,
};
use mqtt_subset::error::CodecError;
use mqtt_subset::packet::Packet;

#[test]
fn decoder_handles_split_packets_byte_by_byte() {
    let frame = encode_puback(0x1234);
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    for (i, b) in frame.iter().enumerate() {
        d.feed(&[*b]);
        if i < frame.len() - 1 {
            assert!(d.try_parse().unwrap().is_none(), "half packet at byte {i}");
        }
    }
    match d.try_parse().unwrap() {
        Some(Packet::Puback(id)) => assert_eq!(id, 0x1234),
        other => panic!("unexpected {other:?}"),
    }
}

#[test]
fn decoder_handles_coalesced_packets() {
    let mut wire = encode_publish("a/b", Some(9), b"hi", 1, false, false);
    wire.extend_from_slice(&encode_puback(10));
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&wire);
    let p1 = d.try_parse().unwrap().expect("first packet");
    let p2 = d.try_parse().unwrap().expect("second packet (sticky)");
    assert!(d.try_parse().unwrap().is_none());
    match p1 {
        Packet::Publish(p) => {
            assert_eq!(p.topic, "a/b");
            assert_eq!(p.packet_id, Some(9));
            assert_eq!(p.payload, b"hi");
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(p2, Packet::Puback(10));
}

#[test]
fn connect_mqtt_311_clean_keepalive_parses() {
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&common::connect_frame("cid", true, 30, 4, false));
    let p = d.try_parse().unwrap().unwrap();
    match p {
        Packet::Connect(c) => {
            assert_eq!(c.client_id, "cid");
            assert!(c.clean_session);
            assert_eq!(c.keep_alive_secs, 30);
            assert!(!c.will_flag);
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn rejects_non_311_protocol_level_via_error() {
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&common::connect_frame("old", true, 30, 3, false));
    assert_eq!(
        d.try_parse().unwrap_err(),
        CodecError::UnsupportedProtocolLevel(3)
    );
}

#[test]
fn malformed_will_flag_combination_is_error() {
    // 手工构造：Will Flag=0 但 Will QoS=1（§3.1.2 一致性冲突）。
    let mut body = Vec::new();
    common::write_mlstr(&mut body, b"MQTT");
    body.push(4);
    body.push(0b0001_0000); // Will QoS=1 (bits4-3), Will Flag=0
    body.extend_from_slice(&60u16.to_be_bytes());
    common::write_mlstr(&mut body, b"cid");
    let mut frame = vec![0x10];
    mqtt_subset::codec::encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);

    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&frame);
    assert!(matches!(
        d.try_parse().unwrap_err(),
        CodecError::MalformedConnect(_)
    ));
}

#[test]
fn invalid_topic_name_characters_rejected() {
    let bad = encode_publish("a/+/b", Some(1), b"x", 1, false, false);
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&bad);
    assert_eq!(d.try_parse().unwrap_err(), CodecError::InvalidTopicName);
}

#[test]
fn subscribe_must_use_reserved_low_bits() {
    // 低 4 位为 0000 而非 0010。
    let good = common::subscribe_frame(1, &[("a/b", 1)]);
    let mut bad = good.clone();
    bad[0] = 0x80;
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&bad);
    assert!(matches!(
        d.try_parse().unwrap_err(),
        CodecError::InvalidReservedFlag(_)
    ));

    let mut d2 = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d2.feed(&good);
    assert!(matches!(
        d2.try_parse().unwrap(),
        Some(Packet::Subscribe(1, _))
    ));
}

#[test]
fn subscribe_qos3_rejected_and_empty_filter_rejected() {
    let mut body = Vec::new();
    body.extend_from_slice(&1u16.to_be_bytes());
    common::write_mlstr(&mut body, b"a/b");
    body.push(3); // QoS 3 非法
    let mut frame = vec![0x82];
    mqtt_subset::codec::encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&frame);
    assert!(matches!(
        d.try_parse().unwrap_err(),
        CodecError::InvalidQoS(3)
    ));

    // 空过滤器列表（只剩 packet id）。
    let empty = vec![0x82, 0x02, 0x00, 0x01];
    let mut d2 = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d2.feed(&empty);
    assert_eq!(
        d2.try_parse().unwrap_err(),
        CodecError::EmptySubscriptionList
    );
}

#[test]
fn packet_id_zero_rejected_for_puback_publish_subscribe() {
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&[0x40, 0x02, 0x00, 0x00]);
    assert!(matches!(
        d.try_parse().unwrap_err(),
        CodecError::MalformedPacket(_)
    ));
}

#[test]
fn truncated_length_prefixed_field_is_error() {
    // CONNECT 声称 ClientID 长度 10，但只给了 2 字节。
    let mut body = Vec::new();
    common::write_mlstr(&mut body, b"MQTT");
    body.push(4);
    body.push(0x02);
    body.extend_from_slice(&60u16.to_be_bytes());
    body.extend_from_slice(&10u16.to_be_bytes());
    body.extend_from_slice(b"ab");
    let mut frame = vec![0x10];
    mqtt_subset::codec::encode_remaining_length(body.len(), &mut frame);
    frame.extend_from_slice(&body);
    let mut d = Decoder::new(DEFAULT_MAX_PACKET_SIZE);
    d.feed(&frame);
    assert!(matches!(
        d.try_parse().unwrap_err(),
        CodecError::LengthExceedsBuffer { .. }
    ));
}

#[test]
fn encoders_produce_expected_wire_bytes() {
    assert_eq!(encode_puback(0x0102), vec![0x40, 0x02, 0x01, 0x02]);
    assert_eq!(
        mqtt_subset::codec::encode_connack(true, 0),
        vec![0x20, 0x02, 0x01, 0x00]
    );
    let suback = encode_suback(0x0102, &[0, 1]);
    assert_eq!(suback, vec![0x90, 0x04, 0x01, 0x02, 0x00, 0x01]);
}
