//! 验收场景 1：正常回声 + Ping 插入数据分片。
mod common;

use common::*;

/// 普通文本/二进制消息原样回声（服务端帧不掩码）。
#[test]
fn echoes_text_and_binary_messages() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &text(true, "Hello, WebSocket!", [0x11, 0x22, 0x33, 0x44]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.opcode, 0x1);
    assert!(f.fin);
    assert_eq!(f.payload, b"Hello, WebSocket!");

    send_byte_by_byte(&mut s, &binary(true, &[0xDE, 0xAD, 0xBE, 0xEF], [7; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.opcode, 0x2);
    assert_eq!(f.payload, vec![0xDE, 0xAD, 0xBE, 0xEF]);
}

/// 文本消息被拆成 3 个分片，中间插入 Ping，服务端必须：
/// 1) 立即回 Pong（带回 ping 的应用数据）；
/// 2) 在收齐 fin 分片后回**一条**完整文本消息。
#[test]
fn ping_inserted_between_fragments_returns_pong_then_full_message() {
    let (mut s, _) = start_server(true, None);

    // 起始帧 fin=0
    send_byte_by_byte(&mut s, &text(false, "Hel", [1; 4]));
    // 插在分片中间的 Ping（含应用数据）
    send_byte_by_byte(&mut s, &ping(b"ping-data", [2; 4]));
    // 结束帧 fin=1
    send_byte_by_byte(&mut s, &cont(true, b"lo!", [3; 4]));

    // 必须先收到 Pong，且内容原样带回
    let pong = read_frame(&mut s).unwrap();
    assert_eq!(pong.opcode, 0xA, "first response must be pong");
    assert_eq!(pong.payload, b"ping-data");

    // 然后是重组完成的完整文本回声
    let echo = read_frame(&mut s).unwrap();
    assert_eq!(echo.opcode, 0x1);
    assert!(echo.fin);
    assert_eq!(echo.payload, b"Hello!");
}

/// 分片中插入多个控制帧：Ping + 未请求的 Pong（应被忽略）+ Ping。
#[test]
fn ping_pong_ping_inside_one_fragmented_message() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &binary(false, &[1, 2], [1; 4]));
    send_byte_by_byte(&mut s, &ping(b"a", [1; 4]));
    // 客户端主动发的 pong 属于未请求 pong，服务端应静默忽略、不回任何东西
    send_byte_by_byte(&mut s, &ctrl(0xA, b"ignored", [1; 4]));
    send_byte_by_byte(&mut s, &ping(b"b", [1; 4]));
    send_byte_by_byte(&mut s, &cont(true, &[3, 4], [1; 4]));

    let p1 = read_frame(&mut s).unwrap();
    assert_eq!((p1.opcode, p1.payload.as_slice()), (0xA, b"a".as_slice()));
    let p2 = read_frame(&mut s).unwrap();
    assert_eq!((p2.opcode, p2.payload.as_slice()), (0xA, b"b".as_slice()));
    let echo = read_frame(&mut s).unwrap();
    assert_eq!((echo.opcode, echo.payload.as_slice()), (0x2, &[1, 2, 3, 4][..]));
}
