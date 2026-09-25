//! WebSocket 帧（RFC 6455 §5.2）的**增量**解析与编码。
//!
//! 解析器 [`FrameReader`] 是一个字节级状态机：可以每次只喂一个字节
//! （`feed_byte`），也可以一次喂一整段（`feed_bytes`）。
//! 在完整帧到达之前，它只保存帧头状态与已收到的部分有效载荷，
//! 不依赖任何现成 WebSocket 库。

use crate::error::Error;

/// RFC 6455 §5.2 定义的操作码。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Opcode {
    Continuation = 0x0,
    Text = 0x1,
    Binary = 0x2,
    Close = 0x8,
    Ping = 0x9,
    Pong = 0xA,
}

impl Opcode {
    /// 将网络字节解析为操作码；保留码（3-7、B-F）返回 [`Error::ReservedOpcode`]。
    pub fn from_u8(value: u8) -> Result<Opcode, Error> {
        match value {
            0x0 => Ok(Opcode::Continuation),
            0x1 => Ok(Opcode::Text),
            0x2 => Ok(Opcode::Binary),
            0x8 => Ok(Opcode::Close),
            0x9 => Ok(Opcode::Ping),
            0xA => Ok(Opcode::Pong),
            other => Err(Error::ReservedOpcode(other)),
        }
    }

    pub fn as_u8(self) -> u8 {
        self as u8
    }

    pub fn is_control(self) -> bool {
        matches!(self, Opcode::Close | Opcode::Ping | Opcode::Pong)
    }
}

/// 一帧已解析完成的数据。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub fin: bool,
    pub opcode: Opcode,
    /// **已去掩码**的有效载荷（控制帧 ≤ 125 字节）。
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn new(fin: bool, opcode: Opcode, payload: Vec<u8>) -> Self {
        Frame {
            fin,
            opcode,
            payload,
        }
    }

    /// 便捷构造：完整（FIN=1）帧。
    pub fn complete(opcode: Opcode, payload: Vec<u8>) -> Self {
        Frame {
            fin: true,
            opcode,
            payload,
        }
    }
}

/// 解析方向：客户端帧必须掩码，服务端帧禁止掩码（§5.1）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PeerRole {
    /// 对端是浏览器/客户端：帧必须带掩码。
    Client,
    /// 对端是服务端：帧禁止带掩码。
    Server,
}

/// 帧头解析状态机的内部阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// 等待第 0 字节（FIN/RSV/opcode）。
    B0,
    /// 等待第 1 字节（MASK/长度）。
    B1,
    /// 读取 16 位扩展长度，剩余字节数在 `len_left`。
    ExtLen16,
    /// 读取 64 位扩展长度，剩余字节数在 `len_left`。
    ExtLen64,
    /// 读取 4 字节掩码，剩余字节数在 `mask_left`。
    Mask,
    /// 读取有效载荷。
    Payload,
}

/// 默认单帧有效载荷上限（1 MiB）。
pub const DEFAULT_MAX_FRAME_PAYLOAD: usize = 1 << 20;

/// 增量帧解析器。
///
/// 典型用法：
/// ```ignore
/// let mut reader = FrameReader::new(PeerRole::Client, 1 << 20);
/// for byte in tcp_bytes() {
///     if let Some(frame) = reader.feed_byte(byte)? {
///         // 拿到一帧完整数据
///     }
/// }
/// ```
#[derive(Debug, Clone)]
pub struct FrameReader {
    role: PeerRole,
    max_payload: usize,
    phase: Phase,
    len_left: u8,
    mask_left: u8,
    fin: bool,
    opcode: Opcode,
    masked: bool,
    payload_len: usize,
    mask: [u8; 4],
    payload: Vec<u8>,
}

impl FrameReader {
    /// `max_payload`：单帧有效载荷字节上限，超过返回 [`Error::FrameTooLarge`]。
    pub fn new(role: PeerRole, max_payload: usize) -> Self {
        FrameReader {
            role,
            max_payload,
            phase: Phase::B0,
            len_left: 0,
            mask_left: 0,
            fin: false,
            opcode: Opcode::Continuation,
            masked: false,
            payload_len: 0,
            mask: [0; 4],
            payload: Vec::new(),
        }
    }

    /// 喂入一个字节。完整帧解析完毕时返回 `Ok(Some(frame))`。
    pub fn feed_byte(&mut self, b: u8) -> Result<Option<Frame>, Error> {
        match self.phase {
            Phase::B0 => {
                let fin = b & 0x80 != 0;
                if b & 0x70 != 0 {
                    return Err(Error::RsvBitsSet);
                }
                let opcode = Opcode::from_u8(b & 0x0F)?;
                // 控制帧不允许分片（§5.5）：FIN 必须为 1。
                if opcode.is_control() && !fin {
                    return Err(Error::FragmentedControlFrame);
                }
                self.fin = fin;
                self.opcode = opcode;
                self.phase = Phase::B1;
                Ok(None)
            }
            Phase::B1 => {
                let masked = b & 0x80 != 0;
                match self.role {
                    PeerRole::Client if !masked => return Err(Error::UnmaskedClientFrame),
                    PeerRole::Server if masked => return Err(Error::MaskedServerFrame),
                    _ => {}
                }
                self.masked = masked;
                let len7 = (b & 0x7F) as usize;

                // 控制帧长度必须 ≤125，即不允许使用扩展长度表示（§5.5）。
                if self.opcode.is_control() && len7 > 125 {
                    return Err(Error::ControlFrameTooLong);
                }
                match len7 {
                    0..=125 => {
                        self.payload_len = len7;
                        self.after_length()
                    }
                    126 => {
                        self.len_left = 2;
                        self.payload_len = 0;
                        self.phase = Phase::ExtLen16;
                        Ok(None)
                    }
                    127 => {
                        self.len_left = 8;
                        self.payload_len = 0;
                        self.phase = Phase::ExtLen64;
                        Ok(None)
                    }
                    _ => unreachable!(),
                }
            }
            Phase::ExtLen16 | Phase::ExtLen64 => {
                self.payload_len = (self.payload_len << 8) | b as usize;
                self.len_left -= 1;
                if self.len_left == 0 {
                    self.after_length()
                } else {
                    Ok(None)
                }
            }
            Phase::Mask => {
                let idx = 4 - self.mask_left as usize;
                self.mask[idx] = b;
                self.mask_left -= 1;
                if self.mask_left == 0 {
                    self.enter_payload()
                } else {
                    Ok(None)
                }
            }
            Phase::Payload => {
                let idx = self.payload.len();
                let decoded = if self.masked {
                    b ^ self.mask[idx % 4]
                } else {
                    b
                };
                self.payload.push(decoded);
                if self.payload.len() == self.payload_len {
                    Ok(Some(self.finish_frame()))
                } else {
                    Ok(None)
                }
            }
        }
    }

    /// 喂入一段字节；可能一次解析出 0..=N 帧。
    pub fn feed_bytes(&mut self, bytes: &[u8]) -> Result<Vec<Frame>, Error> {
        let mut out = Vec::new();
        for &b in bytes {
            if let Some(frame) = self.feed_byte(b)? {
                out.push(frame);
            }
        }
        Ok(out)
    }

    /// 长度字段读完后的统一入口：校验上限，然后进入掩码或载荷阶段。
    fn after_length(&mut self) -> Result<Option<Frame>, Error> {
        if self.payload_len > self.max_payload {
            return Err(Error::FrameTooLarge(self.payload_len));
        }
        if self.opcode.is_control() && self.payload_len > 125 {
            return Err(Error::ControlFrameTooLong);
        }
        if self.masked {
            self.mask_left = 4;
            self.phase = Phase::Mask;
            Ok(None)
        } else {
            self.enter_payload()
        }
    }

    fn enter_payload(&mut self) -> Result<Option<Frame>, Error> {
        self.payload = Vec::with_capacity(self.payload_len.min(1 << 20));
        if self.payload_len == 0 {
            // 零长度帧：不消耗任何载荷字节，立即完成。
            Ok(Some(self.finish_frame()))
        } else {
            self.phase = Phase::Payload;
            Ok(None)
        }
    }

    fn finish_frame(&mut self) -> Frame {
        let frame = Frame {
            fin: self.fin,
            opcode: self.opcode,
            payload: std::mem::take(&mut self.payload),
        };
        self.phase = Phase::B0;
        self.payload_len = 0;
        frame
    }
}

/// 编码一帧（服务端发送：**不掩码**，§5.1）。
pub fn encode_frame(frame: &Frame) -> Vec<u8> {
    encode_frame_masked(frame, None)
}

/// 编码一帧；`mask` 为 `Some(key)` 时带掩码（客户端发送）。
pub fn encode_frame_masked(frame: &Frame, mask: Option<[u8; 4]>) -> Vec<u8> {
    let mut out = Vec::with_capacity(frame.payload.len() + 14);
    let b0 = (if frame.fin { 0x80 } else { 0x00 }) | frame.opcode.as_u8();
    out.push(b0);

    let len = frame.payload.len();
    let mask_bit: u8 = if mask.is_some() { 0x80 } else { 0x00 };
    if len <= 125 {
        out.push(mask_bit | len as u8);
    } else if len <= 0xFFFF {
        out.push(mask_bit | 126);
        out.push((len >> 8) as u8);
        out.push(len as u8);
    } else {
        out.push(mask_bit | 127);
        out.extend_from_slice(&(len as u64).to_be_bytes());
    }

    if let Some(key) = mask {
        out.extend_from_slice(&key);
        for (i, &b) in frame.payload.iter().enumerate() {
            out.push(b ^ key[i % 4]);
        }
    } else {
        out.extend_from_slice(&frame.payload);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 逐字节解析，返回解析出的全部完整帧。
    fn parse_bytewise(role: PeerRole, bytes: &[u8]) -> Result<Vec<Frame>, Error> {
        let mut reader = FrameReader::new(role, 1 << 20);
        let mut frames = Vec::new();
        for &b in bytes {
            if let Some(f) = reader.feed_byte(b)? {
                frames.push(f);
            }
        }
        Ok(frames)
    }

    #[test]
    fn parses_unmasked_short_text() {
        // 服务端风格：FIN text "Hi"
        let raw = [0x81, 0x02, b'H', b'i'];
        let frames = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(frames.len(), 1);
        assert!(frames[0].fin);
        assert_eq!(frames[0].opcode, Opcode::Text);
        assert_eq!(&frames[0].payload, b"Hi");
    }

    #[test]
    fn parses_masked_client_text_bytewise() {
        // 客户端风格：FIN text "Hello"，掩码 0x37FA213D（RFC 6455 §5.7 示例）
        let raw = [
            0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58,
        ];
        let frames = parse_bytewise(PeerRole::Client, &raw).unwrap();
        assert_eq!(&frames[0].payload, b"Hello");
    }

    #[test]
    fn parses_zero_length_frame_without_consuming_next_byte() {
        // 空 ping 紧跟一个文本帧：空帧必须立即完成，不得吃掉下一帧的首字节
        let raw = [0x89, 0x00, 0x81, 0x01, b'z'];
        let frames = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].opcode, Opcode::Ping);
        assert!(frames[0].payload.is_empty());
        assert_eq!(&frames[1].payload, b"z");
    }

    #[test]
    fn parses_16bit_length() {
        let payload = vec![b'x'; 200];
        let mut raw = vec![0x82, 0x7E, 0x00, 0xC8];
        raw.extend_from_slice(&payload);
        let frames = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(frames[0].payload.len(), 200);
        assert_eq!(frames[0].opcode, Opcode::Binary);
    }

    #[test]
    fn parses_64bit_length() {
        let payload = vec![0u8; 70_000];
        let mut raw = vec![0x82, 0x7F];
        raw.extend_from_slice(&(70_000u64).to_be_bytes());
        raw.extend_from_slice(&payload);
        let frames = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(frames[0].payload.len(), 70_000);
    }

    #[test]
    fn parses_multiple_frames_in_one_feed() {
        let raw = [0x81, 0x01, b'a', 0x81, 0x01, b'b'];
        let frames = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(frames.len(), 2);
        assert_eq!(&frames[0].payload, b"a");
        assert_eq!(&frames[1].payload, b"b");
    }

    #[test]
    fn rejects_unmasked_client_frame() {
        let raw = [0x81, 0x02, b'H', b'i'];
        let err = parse_bytewise(PeerRole::Client, &raw).unwrap_err();
        assert_eq!(err, Error::UnmaskedClientFrame);
    }

    #[test]
    fn rejects_masked_server_frame() {
        let raw = [0x81, 0x85, 1, 2, 3, 4, 0, 0, 0, 0, 0];
        assert_eq!(
            parse_bytewise(PeerRole::Server, &raw).unwrap_err(),
            Error::MaskedServerFrame
        );
    }

    #[test]
    fn rejects_rsv_bits() {
        let raw = [0xF1, 0x00];
        assert_eq!(
            parse_bytewise(PeerRole::Server, &raw).unwrap_err(),
            Error::RsvBitsSet
        );
    }

    #[test]
    fn rejects_reserved_opcode() {
        let raw = [0x83, 0x00];
        assert!(matches!(
            parse_bytewise(PeerRole::Server, &raw).unwrap_err(),
            Error::ReservedOpcode(3)
        ));
    }

    #[test]
    fn rejects_fragmented_control_frame() {
        // FIN=0 的 ping
        let raw = [0x09, 0x00];
        assert_eq!(
            parse_bytewise(PeerRole::Server, &raw).unwrap_err(),
            Error::FragmentedControlFrame
        );
    }

    #[test]
    fn rejects_control_frame_with_extended_length() {
        // 控制帧长度位为 126（即使用扩展长度）
        let raw = [0x89, 0x7E, 0x00, 0x7E];
        assert_eq!(
            parse_bytewise(PeerRole::Server, &raw).unwrap_err(),
            Error::ControlFrameTooLong
        );
    }

    #[test]
    fn rejects_oversized_frame_at_declaration() {
        // 限制为 10 字节，但声明长度 100：在读到长度时即拒绝（不缓冲数据）
        let mut reader = FrameReader::new(PeerRole::Server, 10);
        assert_eq!(reader.feed_byte(0x81).unwrap(), None);
        assert_eq!(reader.feed_byte(0x7E).unwrap(), None);
        assert_eq!(reader.feed_byte(0x00).unwrap(), None);
        assert_eq!(
            reader.feed_byte(0x64).unwrap_err(),
            Error::FrameTooLarge(100)
        );
    }

    #[test]
    fn roundtrip_masked_encode_decode() {
        let frame = Frame::complete(Opcode::Text, b"abcdef".to_vec());
        let raw = encode_frame_masked(&frame, Some([0x12, 0x34, 0x56, 0x78]));
        let decoded = parse_bytewise(PeerRole::Client, &raw).unwrap();
        assert_eq!(decoded[0], frame);
    }

    #[test]
    fn roundtrip_unmasked_encode_decode() {
        let frame = Frame::complete(Opcode::Binary, vec![0, 1, 2, 255]);
        let raw = encode_frame(&frame);
        let decoded = parse_bytewise(PeerRole::Server, &raw).unwrap();
        assert_eq!(decoded[0], frame);
    }
}
