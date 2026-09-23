//! RFC 6455 §5.2 基础帧协议层：帧头解析、掩码去除、服务端帧编码、Close 帧解析。
//!
//! 本层**无状态**：一次只解析一个完整帧头，不缓存字节、不管分片。
//! 增量喂入与消息重组在 [`crate::reassemble`] 中完成。

use crate::error::{is_valid_close_code, WsError, WsResult};

/// RFC 6455 §5.2 操作码（opcode，4 bit）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Opcode {
    /// 0x0：分片续帧。
    Continuation,
    /// 0x1：文本帧（载荷必须是 UTF-8）。
    Text,
    /// 0x2：二进制帧。
    Binary,
    /// 0x8：关闭。
    Close,
    /// 0x9：Ping。
    Ping,
    /// 0xA：Pong。
    Pong,
    /// RFC 保留 / 未知操作码（0x3–0x7、0xB–0xF）。
    Reserved(u8),
}

impl Opcode {
    /// 从线上 4 bit 值构造。
    pub fn from_u4(v: u8) -> Opcode {
        match v {
            0 => Opcode::Continuation,
            1 => Opcode::Text,
            2 => Opcode::Binary,
            8 => Opcode::Close,
            9 => Opcode::Ping,
            10 => Opcode::Pong,
            other => Opcode::Reserved(other),
        }
    }

    /// 转回线上 4 bit 值。
    pub fn as_u8(self) -> u8 {
        match self {
            Opcode::Continuation => 0,
            Opcode::Text => 1,
            Opcode::Binary => 2,
            Opcode::Close => 8,
            Opcode::Ping => 9,
            Opcode::Pong => 10,
            Opcode::Reserved(v) => v,
        }
    }

    /// 是否为控制帧（opcode 最高位为 1，即 0x8–0xF）。
    pub fn is_control(self) -> bool {
        self.as_u8() >= 0x8
    }
}

/// 长度上限配置（字节）。解析期就会拒绝超限帧。
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// 单个帧 payload 长度上限（含控制帧，控制帧另有 125 字节硬上限）。
    pub max_frame_payload: u64,
    /// 一条重组消息（跨所有分片）的总长度上限。
    pub max_message_size: usize,
}

impl Limits {
    /// 与服务端默认值一致：帧 1 MiB、消息 64 KiB（刻意让“消息比帧小”，
    /// 方便在常规环境下演示 1009 关闭码）。
    pub const DEFAULT: Limits = Limits {
        max_frame_payload: 1 << 20,
        max_message_size: 1 << 16,
    };
}

/// 解析后的帧头。掩码密钥只在去掩码时短暂使用，不对外长期保存。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameHeader {
    pub fin: bool,
    pub rsv1: bool,
    pub rsv2: bool,
    pub rsv3: bool,
    pub opcode: Opcode,
    /// 线上原始 opcode，便于报告未知操作码。
    pub opcode_raw: u8,
    pub masked: bool,
    pub payload_len: u64,
    /// 帧头总字节数（2/4/10），payload 紧跟其后。
    pub header_len: usize,
}

/// 读取/校验顺序严格按 RFC 6455 §5.2、§5.5、§7.4.1 执行。
///
/// 入参 `buf` 必须包含**完整帧头**（是否完整由调用方按返回的需求长度判断）。
/// 本函数只做帧头；payload 去掩码由 [`unmask_payload`] 完成。
pub fn parse_header(buf: &[u8], limits: Limits) -> WsResult<FrameHeader> {
    // §5.2：帧头最少 2 字节。
    if buf.len() < 2 {
        return Err(WsError::Incomplete);
    }
    let b0 = buf[0];
    let b1 = buf[1];

    let fin = b0 & 0x80 != 0;
    let rsv1 = b0 & 0x40 != 0;
    let rsv2 = b0 & 0x20 != 0;
    let rsv3 = b0 & 0x10 != 0;
    let opcode_raw = b0 & 0x0F;

    let masked = b1 & 0x80 != 0;
    let len7 = b1 & 0x7F;

    // §5.2：没有协商扩展时三个 RSV 位必须为 0（1002）。
    if rsv1 || rsv2 || rsv3 {
        return Err(WsError::RsvBitsSet);
    }

    // §5.1：客户端发给服务端的帧必须置掩码位（1002）。
    if !masked {
        return Err(WsError::FrameNotMasked);
    }

    let opcode = Opcode::from_u4(opcode_raw);

    let (payload_len, header_len) = match len7 {
        // 长度直接就是 0..=125。
        0..=125 => (u64::from(len7), 2),
        // 随后 2 字节是 16 位无符号长度。
        126 => {
            if buf.len() < 4 {
                return Err(WsError::Incomplete);
            }
            (u64::from(u16::from_be_bytes([buf[2], buf[3]])), 4)
        }
        // 随后 8 字节是 64 位无符号长度。
        127 => {
            if buf.len() < 10 {
                return Err(WsError::Incomplete);
            }
            let mut arr = [0u8; 8];
            arr.copy_from_slice(&buf[2..10]);
            (u64::from_be_bytes(arr), 10)
        }
        _ => unreachable!("len7 masked with 0x7F, max is 127"),
    };

    // 掩码密钥紧随长度字段，解析器在重组层校验其存在性。
    let header_len = header_len + 4;
    if buf.len() < header_len {
        return Err(WsError::Incomplete);
    }

    // §5.2 保留操作码（3..=7 数据类、0xB..=0xF 控制类）一律拒绝（1002）。
    if let Opcode::Reserved(op) = opcode {
        return Err(WsError::UnknownOpcode(op));
    }

    // §5.5.1：控制帧 payload 不得超过 125 字节，且不得分片。
    // 先于通用长度上限检查，错误类型更精确。
    if opcode.is_control() {
        if payload_len > 125 {
            return Err(WsError::ControlFrameTooLong(payload_len));
        }
        if !fin {
            return Err(WsError::ControlFrameFragmented);
        }
    }

    if payload_len > limits.max_frame_payload {
        return Err(WsError::FrameTooLarge(payload_len));
    }

    Ok(FrameHeader {
        fin,
        rsv1,
        rsv2,
        rsv3,
        opcode,
        opcode_raw,
        masked,
        payload_len,
        header_len,
    })
}

/// 从帧头之后读取 4 字节掩码密钥并就地解除掩码（RFC 6455 §5.3）。
///
/// `frame` 为「扩展长度字段之后、含掩码密钥的整段缓冲」：
/// 前 4 字节是 masking key，其后是 payload。
pub fn unmask_payload(frame: &mut [u8]) {
    debug_assert!(frame.len() >= 4, "caller must ensure masking key present");
    let mut key = [0u8; 4];
    key.copy_from_slice(&frame[..4]);
    let payload = &mut frame[4..];
    for (i, b) in payload.iter_mut().enumerate() {
        *b ^= key[i % 4];
    }
}

/// 解析 Close 帧载荷：`[u16 状态码][UTF-8 原因]`（§5.5.1 / §7.4）。
///
/// - 空载荷：对端未给状态码，合法（本服务回复时用 1000）；
/// - 1 字节：半个状态码，非法；
/// - 状态码非法/保留，或原因不是 UTF-8：返回 [`WsError::InvalidCloseFrame`]。
pub fn parse_close_payload(payload: &[u8]) -> WsResult<Option<(u16, Option<&str>)>> {
    match payload.len() {
        0 => Ok(None),
        1 => Err(WsError::InvalidCloseFrame),
        n => {
            let code = u16::from_be_bytes([payload[0], payload[1]]);
            if !is_valid_close_code(code) {
                return Err(WsError::InvalidCloseFrame);
            }
            // 允许空原因；有原因则必须是合法 UTF-8。
            if n > 2 {
                match std::str::from_utf8(&payload[2..]) {
                    Ok(reason) => Ok(Some((code, Some(reason)))),
                    Err(_) => Err(WsError::InvalidCloseFrame),
                }
            } else {
                Ok(Some((code, None)))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 服务端帧编码（发送方向不掩码，mask 位为 0，§5.3）
// ---------------------------------------------------------------------------

fn push_header(out: &mut Vec<u8>, fin: bool, opcode: u8, len: usize) {
    out.push(if fin { 0x80 | opcode } else { opcode });
    match len {
        0..=125 => out.push(len as u8),
        126..=65535 => {
            out.push(126);
            out.extend_from_slice(&(len as u16).to_be_bytes());
        }
        _ => {
            out.push(127);
            out.extend_from_slice(&(len as u64).to_be_bytes());
        }
    }
}

fn write_frame(out: &mut Vec<u8>, opcode: u8, fin: bool, payload: &[u8]) {
    push_header(out, fin, opcode, payload.len());
    out.extend_from_slice(payload);
}

/// 服务端文本帧（fin）。
pub fn write_text(out: &mut Vec<u8>, payload: &str) {
    write_frame(out, Opcode::Text.as_u8(), true, payload.as_bytes());
}

/// 服务端二进制帧（fin）。
pub fn write_binary(out: &mut Vec<u8>, payload: &[u8]) {
    write_frame(out, Opcode::Binary.as_u8(), true, payload);
}

/// 服务端 Ping（payload ≤ 125）。
pub fn write_ping(out: &mut Vec<u8>, payload: &[u8]) {
    debug_assert!(payload.len() <= 125);
    write_frame(out, Opcode::Ping.as_u8(), true, payload);
}

/// 服务端 Pong（回带 Ping 的应用数据，§5.5.3）。
pub fn write_pong(out: &mut Vec<u8>, payload: &[u8]) {
    debug_assert!(payload.len() <= 125);
    write_frame(out, Opcode::Pong.as_u8(), true, payload);
}

/// 服务端 Close 帧；`payload` 需调用方自行保证是 `[code BE][utf8 reason]`。
pub fn write_close_raw(out: &mut Vec<u8>, payload: &[u8]) {
    debug_assert!(payload.len() <= 125);
    write_frame(out, Opcode::Close.as_u8(), true, payload);
}

/// 构造带状态码与原因的 Close 载荷（总长 ≤ 125，超长时原因被截断到字符边界）。
pub fn close_payload(code: u16, reason: &str) -> Vec<u8> {
    let mut v = Vec::with_capacity(2 + reason.len());
    v.extend_from_slice(&code.to_be_bytes());
    let mut bytes = reason.as_bytes();
    while 2 + bytes.len() > 125 && !bytes.is_empty() {
        let mut cut = bytes.len() - 1;
        while cut > 0 && (bytes[cut] & 0xC0) == 0x80 {
            cut -= 1;
        }
        bytes = &bytes[..cut];
    }
    v.extend_from_slice(bytes);
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    const L: Limits = Limits::DEFAULT;

    #[test]
    fn parses_masked_short_text_frame() {
        // 客户端：fin text, masked, len=5, key=0x37, "hello" 掩码后
        let mut frame = vec![0x81, 0x85, 0x37, 0x37, 0x37, 0x37];
        let masked_payload: [u8; 5] = [
            b'h' ^ 0x37,
            b'e' ^ 0x37,
            b'l' ^ 0x37,
            b'l' ^ 0x37,
            b'o' ^ 0x37,
        ];
        frame.extend_from_slice(&masked_payload);
        let h = parse_header(&frame, L).unwrap();
        assert_eq!(h.opcode, Opcode::Text);
        assert!(h.fin);
        assert!(h.masked);
        assert_eq!(h.payload_len, 5);
        assert_eq!(h.header_len, 6);
        unmask_payload(&mut frame[2..]);
        assert_eq!(&frame[6..], b"hello");
    }

    #[test]
    fn rejects_unmasked_client_frame() {
        let frame = [0x81, 0x05, b'h', b'e', b'l', b'l', b'o'];
        assert_eq!(parse_header(&frame, L), Err(WsError::FrameNotMasked));
    }

    #[test]
    fn rejects_rsv_bits() {
        // rsv1=1, text, masked, len=0
        assert_eq!(
            parse_header(&[0xC1, 0x80, 0, 0, 0, 0], L),
            Err(WsError::RsvBitsSet)
        );
    }

    #[test]
    fn rejects_unknown_data_and_control_opcodes() {
        // opcode 3（保留数据帧）
        assert_eq!(
            parse_header(&[0x83, 0x80, 0, 0, 0, 0], L),
            Err(WsError::UnknownOpcode(3))
        );
        // opcode 0xB（保留控制帧）
        assert_eq!(
            parse_header(&[0x8B, 0x80, 0, 0, 0, 0], L),
            Err(WsError::UnknownOpcode(0xB))
        );
    }

    #[test]
    fn rejects_fragmented_control_frame() {
        // fin=0 ping, masked, len=0
        assert_eq!(
            parse_header(&[0x09, 0x80, 0, 0, 0, 0], L),
            Err(WsError::ControlFrameFragmented)
        );
    }

    #[test]
    fn rejects_control_frame_over_125() {
        // fin ping，16 位长度 126
        let mut f = vec![0x89, 0xFE, 0x00, 0x7E, 0, 0, 0, 0];
        assert_eq!(
            parse_header(&f, L),
            Err(WsError::ControlFrameTooLong(126))
        );
        // 64 位长度 256
        f = vec![0x89, 0xFF, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0];
        assert_eq!(
            parse_header(&f, L),
            Err(WsError::ControlFrameTooLong(256))
        );
    }

    #[test]
    fn rejects_oversized_data_frame() {
        // text masked，16 位长度 65535 > 1MiB? 不，65535 < 1MiB。改用 64 位 2MiB。
        let f = vec![
            0x81, 0x80 | 127, 0, 0, 0, 0, 0, 0x20, 0, 0, // len=2MiB
            0, 0, 0, 0,
        ];
        assert_eq!(
            parse_header(&f, L),
            Err(WsError::FrameTooLarge(2 * 1024 * 1024))
        );
    }

    #[test]
    fn header_needs_more_bytes_is_incomplete() {
        assert_eq!(parse_header(&[0x81], L), Err(WsError::Incomplete));
        // 声明了 16 位长度但没给够
        assert_eq!(
            parse_header(&[0x81, 0xFE, 0x00], L),
            Err(WsError::Incomplete)
        );
        // 长度字段有了，但缺掩码密钥
        assert_eq!(
            parse_header(&[0x81, 0x85, 0, 0], L),
            Err(WsError::Incomplete)
        );
    }

    #[test]
    fn extended_16_and_64_lengths_parse() {
        let h = parse_header(&[0x82, 0xFE, 0x01, 0x00, 1, 2, 3, 4], L).unwrap();
        assert_eq!(h.payload_len, 256);
        assert_eq!(h.header_len, 8);

        let h = parse_header(
            &[0x82, 0xFF, 0, 0, 0, 0, 0, 0, 0x10, 0, 1, 2, 3, 4],
            L,
        )
        .unwrap();
        assert_eq!(h.payload_len, 4096);
        assert_eq!(h.header_len, 14);
    }

    #[test]
    fn close_payload_validation() {
        assert_eq!(parse_close_payload(&[]), Ok(None));
        assert_eq!(
            parse_close_payload(&[0x03]),
            Err(WsError::InvalidCloseFrame)
        );
        // 1000 + "bye"
        assert_eq!(
            parse_close_payload(&[0x03, 0xE8, b'b', b'y', b'e']).unwrap(),
            Some((1000, Some("bye")))
        );
        // 保留码 1005 不得出现在帧里
        assert_eq!(
            parse_close_payload(&[0x03, 0xED]),
            Err(WsError::InvalidCloseFrame)
        );
        // 私有码 3000 合法
        assert_eq!(
            parse_close_payload(&[0x0B, 0xB8]).unwrap(),
            Some((3000, None))
        );
        // 1000 + 非法 UTF-8 原因
        assert_eq!(
            parse_close_payload(&[0x03, 0xE8, 0xFF]),
            Err(WsError::InvalidCloseFrame)
        );
    }

    #[test]
    fn server_frame_encoding_roundtrip_lengths() {
        let mut out = Vec::new();
        write_text(&mut out, "hi");
        assert_eq!(out, vec![0x81, 0x02, b'h', b'i']);

        let mut out = Vec::new();
        write_close_raw(&mut out, &close_payload(1000, "bye"));
        assert_eq!(out[0], 0x88);
        assert_eq!(out[1], 5);
        assert_eq!(&out[2..4], &1000u16.to_be_bytes());
        assert_eq!(&out[4..], b"bye");
    }
}
