//! 验收场景 5：真实 TCP 上严格逐字节喂入——帧头、扩展长度字段、
//! 载荷都可以在任意字节边界停顿，服务端必须从断点继续。
mod common;

use std::io::{Read, Write};

use common::*;

/// 把一个完整握手后的会话，按**一个字节一 flush** 的方式发送，
/// 中途每次只发一个字节并等待，最终仍应正确回声。
#[test]
fn frame_arriving_one_byte_at_a_time_is_reassembled() {
    let (mut s, _) = start_server(true, None);
    let bytes = text(true, "incremental", [0xAB; 4]);
    send_byte_by_byte(&mut s, &bytes);
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.payload, b"incremental");
}

/// 16 位扩展长度帧（200 字节）逐字节到达，长度字段本身也被拆开。
#[test]
fn extended_16bit_length_one_byte_at_a_time() {
    let (mut s, _) = start_server(true, None);
    let payload = vec![b'x'; 200];
    let bytes = binary(true, &payload, [0x5A; 4]);
    send_byte_by_byte(&mut s, &bytes);
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.opcode, 0x2);
    assert_eq!(f.payload.len(), 200);
    assert!(f.payload.iter().all(|&b| b == b'x'));
}

/// 帧头只发一半就停住，服务端不应有任何响应；补齐后再正常回声。
#[test]
fn partial_header_silence_then_completion() {
    let (mut s, _) = start_server(true, None);
    s.set_read_timeout(Some(std::time::Duration::from_millis(300)))
        .unwrap();
    let bytes = text(true, "AB", [1; 4]);
    // 只发 3 个字节（帧头 6 字节都不够）
    for &b in &bytes[..3] {
        s.write_all(&[b]).unwrap();
        s.flush().unwrap();
    }
    // 此时读应超时（服务端不能误发响应或关闭）
    let mut buf = [0u8; 1];
    let timed_out = s.read(&mut buf).is_err();
    assert!(timed_out, "server must stay silent with a partial header");

    // 恢复较长读超时，补齐剩余字节
    s.set_read_timeout(Some(std::time::Duration::from_secs(3))).unwrap();
    send_byte_by_byte(&mut s, &bytes[3..]);
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.payload, b"AB");
}

/// 分片消息中，前一帧载荷发一半时不触发任何回声。
#[test]
fn partial_payload_does_not_flush_message() {
    let (mut s, _) = start_server(true, None);
    s.set_read_timeout(Some(std::time::Duration::from_millis(300)))
        .unwrap();
    // 起始帧 fin=0，4 字节载荷；只发帧头 + 2 个载荷字节
    let first = text(false, "abcd", [1; 4]);
    let split = 6 + 2; // 帧头 6 字节 + 2 字节掩码后载荷
    s.write_all(&first[..split]).unwrap();
    s.flush().unwrap();
    let mut buf = [0u8; 1];
    assert!(s.read(&mut buf).is_err(), "partial fragment must be silent");

    s.set_read_timeout(Some(std::time::Duration::from_secs(3))).unwrap();
    send_byte_by_byte(&mut s, &first[split..]);
    send_byte_by_byte(&mut s, &cont(true, b"ef", [2; 4]));
    let f = read_frame(&mut s).unwrap();
    assert_eq!(f.payload, b"abcdef");
}
