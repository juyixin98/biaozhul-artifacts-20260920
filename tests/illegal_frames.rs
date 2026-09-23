//! 验收场景 2：非法分片序列必须以 1002 关闭。
mod common;

use common::*;

/// 连接开始就来 continuation（没有起始帧）。
#[test]
fn continuation_without_start_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &cont(true, b"x", [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(close.opcode, 0x8);
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// 分片消息未结束时，又来一个 text/binary 起始帧（fin 或非 fin 都非法）。
#[test]
fn second_start_while_fragment_open_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &text(false, "ab", [1; 4]));
    send_byte_by_byte(&mut s, &text(true, "cd", [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(close.opcode, 0x8);
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// 控制帧不允许分片（fin=0 的 ping）→ 1002。
#[test]
fn fragmented_control_frame_is_1002() {
    let (mut s, _) = start_server(true, None);
    // 手工构造：fin=0, opcode=0x9(ping), masked, len=0
    send_byte_by_byte(&mut s, &[0x09, 0x80, 0, 0, 0, 0]);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// 保留操作码（0x3 / 0xB）→ 1002。
#[test]
fn reserved_opcode_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(true, 0x3, b"x", [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// RSV1 置位且未协商扩展 → 1002。
#[test]
fn rsv_bit_set_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &[0xC1, 0x80, 0, 0, 0, 0]);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// 未掩码客户端帧 → 1002。
#[test]
fn unmasked_client_frame_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &[0x81, 0x02, b'h', b'i']);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}
