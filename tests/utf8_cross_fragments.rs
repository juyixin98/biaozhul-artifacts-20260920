//! 验收场景 3：跨分片 UTF-8 校验，覆盖「半个 UTF-8 字符」。
mod common;

use common::*;

/// 一个 3 字节汉字“你”（E4 BD A0）被切成 2+1 跨两帧：必须重组成功。
#[test]
fn half_multibyte_char_split_across_frames_is_valid() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(false, 0x1, &[0xE4, 0xBD], [1; 4]));
    send_byte_by_byte(&mut s, &cont(true, &[0xA0], [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.opcode, 0x1);
    assert_eq!(f.payload, "你".as_bytes());
}

/// 4 字节字符 U+1F600（F0 9F 98 80）切成 1+1+2 跨 3 帧（中间还夹着 ping）。
#[test]
fn four_byte_char_split_three_ways_with_ping_in_between() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(false, 0x1, &[0xF0], [1; 4]));
    send_byte_by_byte(&mut s, &ping(&[], [9; 4]));
    let pong = read_frame(&mut s).unwrap();
    assert_eq!(pong.opcode, 0xA);
    send_byte_by_byte(&mut s, &cont(false, &[0x9F], [1; 4]));
    send_byte_by_byte(&mut s, &cont(true, &[0x98, 0x80], [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.payload, "😀".as_bytes());
}

/// 单帧 fin 文本末尾停在半个字符 → 1007。
#[test]
fn truncated_char_in_complete_frame_is_1007() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(true, 0x1, &[0xE4, 0xBD], [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(close.opcode, 0x8);
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1007);
}

/// 分片消息结束时仍差一个续字节 → 1007。
#[test]
fn truncated_char_at_fin_continuation_is_1007() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(false, 0x1, &[0xE4], [1; 4]));
    send_byte_by_byte(&mut s, &cont(true, &[0xBD], [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1007);
}

/// 消息内部出现硬非法字节（0xFF）→ 服务端在收到该续帧时立即 1007。
#[test]
fn invalid_byte_inside_message_is_1007() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(false, 0x1, b"a", [1; 4]));
    // 含 0xFF 的非 fin 续帧：服务端在该帧完整、喂入 UTF-8 校验器时即拒绝，
    // 不应再发送后续帧（连接此刻已进入关闭流程）。
    send_all(&mut s, &cont(false, &[0xFF], [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1007);
}

/// 代理区 U+D800（ED A0 80）与超长编码（C0 80）都必须拒绝 → 1007。
#[test]
fn surrogate_and_overlong_are_1007() {
    for bad in [&[0xED, 0xA0, 0x80][..], &[0xC0, 0x80][..]] {
        let (mut s, _) = start_server(true, None);
        send_byte_by_byte(&mut s, &frame(true, 0x1, bad, [1; 4]));
        let close = read_frame(&mut s).unwrap();
        assert_eq!(
            u16::from_be_bytes([close.payload[0], close.payload[1]]),
            1007,
            "bytes {bad:?} must be invalid utf-8"
        );
    }
}
