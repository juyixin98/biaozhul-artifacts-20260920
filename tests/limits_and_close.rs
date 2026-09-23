//! 验收场景 4：超长消息（1009）、控制帧长度（1002）、关闭码检查（1000/保留码）。
mod common;

use common::*;
use wsframe::frame::Limits;

/// 分片重组后超过 max_message_size → 1009。
#[test]
fn fragmented_message_over_limit_is_1009() {
    let limits = Limits {
        max_frame_payload: 1 << 20,
        max_message_size: 64,
    };
    let (mut s, _) = start_server(true, Some(limits));
    // 起始帧 40 字节（未超），续帧 30 字节，合计 70 > 64。
    // 注意：消息级校验在「整帧载荷收齐」时执行，因此续帧必须发全；
    // 用一次批量写完成，避免逐字节发送时撞上服务端随后的关闭。
    send_byte_by_byte(&mut s, &text(false, &"a".repeat(40), [1; 4]));
    let cont_full = cont(true, b"b".repeat(30).as_ref(), [1; 4]);
    send_all(&mut s, &cont_full);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(close.opcode, 0x8);
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1009);
}

/// 单帧超过 max_frame_payload（帧头层即拒绝，无需等载荷收完）→ 1009。
#[test]
fn single_frame_over_frame_limit_is_1009() {
    let limits = Limits {
        max_frame_payload: 16,
        max_message_size: 1 << 20,
    };
    let (mut s, _) = start_server(true, Some(limits));
    let f = binary(true, &[0u8; 100], [1; 4]);
    // 帧头声明 100 字节 > 16：帧头层即拒绝，只发帧头（16 位长度字段+掩码密钥，8 字节）。
    send_all(&mut s, &f[..header_len(100)]);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1009);
}

/// 默认限制（消息 64KiB）下的超大消息端到端 → 1009。
#[test]
fn default_limit_huge_message_is_1009() {
    let (mut s, _) = start_server(true, None);
    // 单帧 70000 字节：帧上限 1MiB 允许，但消息上限 64KiB 拒绝。
    // 批量发送（消息级判定在整帧收齐后）；逐字节增量路径已在其它用例覆盖。
    send_all(&mut s, &binary(true, &vec![0u8; 70_000], [1; 4]));
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1009);
}

/// 控制帧载荷 > 125 字节 → 1002（而不是 1009）。
#[test]
fn control_frame_over_125_bytes_is_1002() {
    let (mut s, _) = start_server(true, None);
    let f = frame(true, 0x9, &[0u8; 126], [1; 4]);
    // 服务端在帧头（声明长度 126）完整时即判 1002，不必发载荷。
    send_all(&mut s, &f[..header_len(126)]);
    let close = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([close.payload[0], close.payload[1]]), 1002);
}

/// 正常关闭：客户端发 1000，服务端应回 1000。
#[test]
fn normal_close_1000_is_echoed() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &close(Some(1000), "bye", [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.opcode, 0x8);
    assert_eq!(u16::from_be_bytes([f.payload[0], f.payload[1]]), 1000);
    // 服务端回完关闭帧后应关闭连接
    assert!(read_frame_or_none(&mut s).is_none(), "server must close TCP after close");
}

/// 空载荷关闭帧：服务端以 1000 回复。
#[test]
fn empty_close_gets_1000_reply() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &close(None, "", [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([f.payload[0], f.payload[1]]), 1000);
}

/// 保留码 1005 绝不能出现在关闭帧中 → 服务端 1002。
#[test]
fn reserved_close_code_1005_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &close(Some(1005), "", [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([f.payload[0], f.payload[1]]), 1002);
}

/// 私有码 3000 合法，服务端应原样回复。
#[test]
fn private_close_code_3000_is_echoed() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &close(Some(3000), "cu", [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([f.payload[0], f.payload[1]]), 3000);
    assert_eq!(&f.payload[2..], b"cu");
}

/// 关闭帧只有 1 字节载荷（半个状态码）→ 1002。
#[test]
fn one_byte_close_payload_is_1002() {
    let (mut s, _) = start_server(true, None);
    send_byte_by_byte(&mut s, &frame(true, 0x8, &[0x03], [1; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(u16::from_be_bytes([f.payload[0], f.payload[1]]), 1002);
}
