//! 核心重组状态机：把**逐个字节**喂入的客户端数据解析为帧、再重组为消息。
//!
//! 设计要点（全部依据 RFC 6455）：
//! - 输入 API 是 [`Reassembler::feed`]，一次一个字节；调用方可以真的逐字节喂，
//!   也可用 [`Reassembler::feed_slice`] 批量喂（内部仍是逐字节推进）。
//! - 帧头未到齐时返回 [`WsError::Incomplete`]（经 `Ok(None)` 表达“暂无事件”），
//!   新字节到达后从断点继续，不丢任何状态。
//! - 掩码、RSV、操作码、控制帧长度/分片在帧头层拒绝（见 [`crate::frame`]）。
//! - 分片序列（§5.4）与控制帧插入（§5.5）由本层状态机维护。
//! - 文本消息的 UTF-8 校验**跨分片续写**（§5.6），允许半个多字节字符落在帧边界。
//! - 长度上限双重约束：单帧 `max_frame_payload`（帧头层）、整消息
//!   `max_message_size`（本层，追加前判定）。

use std::mem;

use crate::error::{WsError, WsResult};
use crate::frame::{parse_close_payload, parse_header, unmask_payload, FrameHeader, Limits, Opcode};
use crate::utf8::IncrementalUtf8;

/// 重组完成后向上层交付的事件（载荷为已去掩码、已重组的数据）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// 一条完整文本消息（保证合法 UTF-8）。
    Text(String),
    /// 一条完整二进制消息。
    Binary(Vec<u8>),
    /// 收到 Ping（服务端应回 Pong，原样带回应用数据）。
    Ping(Vec<u8>),
    /// 收到 Pong（可在分片消息进行中到达，不影响分片状态）。
    Pong(Vec<u8>),
    /// 收到 Close；`code/reason` 为 `None` 表示对端发了空载荷关闭帧。
    Close {
        code: Option<u16>,
        reason: Option<String>,
    },
}

#[derive(Debug, PartialEq, Eq)]
enum Stage {
    /// 正在攒帧头（含扩展长度与掩码密钥）。
    Header,
    /// 帧头完整，正在攒 payload。
    Payload,
}

#[derive(Debug)]
enum Frag {
    /// 没有进行中的分片消息。
    Idle,
    /// 进行中的文本消息，携带跨分片 UTF-8 校验器。
    Text(IncrementalUtf8),
    /// 进行中的二进制消息。
    Binary,
}

/// 增量解析/重组器。每个 TCP 连接创建一个。
#[derive(Debug)]
pub struct Reassembler {
    limits: Limits,
    buf: Vec<u8>,
    stage: Stage,
    header: Option<FrameHeader>,
    frag: Frag,
    /// 当前分片消息已累积的载荷。
    message: Vec<u8>,
    /// 是否已进入关闭握手；之后任何字节都是协议错误。
    closed: bool,
}

impl Reassembler {
    pub fn new(limits: Limits) -> Self {
        Self {
            limits,
            buf: Vec::new(),
            stage: Stage::Header,
            header: None,
            frag: Frag::Idle,
            message: Vec::new(),
            closed: false,
        }
    }

    /// 使用 [`Limits::DEFAULT`]。
    pub fn new_default() -> Self {
        Self::new(Limits::DEFAULT)
    }

    /// 喂入一个字节。
    ///
    /// - `Ok(None)`：字节已收下，但还构不成完整事件（帧头/载荷未齐）；
    /// - `Ok(Some(ev))`：产生了一个事件；
    /// - `Err(..)`：协议致命错误，调用方应按 [`WsError::close_code`] 关闭连接。
    pub fn feed(&mut self, byte: u8) -> WsResult<Option<Event>> {
        // §5.5.1：Close 握手完成后不允许再出现数据帧。
        if self.closed {
            return Err(WsError::ConnectionClosed);
        }
        self.buf.push(byte);
        self.advance()
    }

    /// 批量喂入（内部严格逐字节推进），收集本次产生的全部事件。
    pub fn feed_slice(&mut self, bytes: &[u8]) -> WsResult<Vec<Event>> {
        let mut events = Vec::new();
        for &b in bytes {
            if let Some(ev) = self.feed(b)? {
                events.push(ev);
            }
        }
        Ok(events)
    }

    /// 当前是否在等待/组装某条消息的后续分片。
    pub fn is_fragmented_message_open(&self) -> bool {
        !matches!(self.frag, Frag::Idle)
    }

    /// 连接是否已收到 Close 帧。
    pub fn is_closed(&self) -> bool {
        self.closed
    }

    /// 缓冲驱动：根据所处阶段尝试继续解析。
    fn advance(&mut self) -> WsResult<Option<Event>> {
        // 逐字节喂入时，一次调用最多补全一个帧；这里用循环处理「零载荷帧」：
        // 最后一个帧头字节到达的同一拍，payload 长度为 0，应立即交付事件。
        loop {
            match self.stage {
                Stage::Header => {
                    match parse_header(&self.buf, self.limits) {
                        Ok(h) => {
                            // 帧头已完整（含掩码密钥）。校验长度能否被平台容纳。
                            if u64::try_from(usize::MAX).is_ok_and(|m| h.payload_len > m) {
                                return Err(WsError::LengthExceedsPlatform);
                            }
                            self.header = Some(h);
                            self.stage = Stage::Payload;
                            // 落到 Payload 分支：零载荷帧当场完成。
                        }
                        Err(WsError::Incomplete) => return Ok(None),
                        Err(e) => return Err(e),
                    }
                }
                Stage::Payload => {
                    let header_len = self.header.as_ref().unwrap().header_len;
                    let payload_len = self.header.as_ref().unwrap().payload_len;
                    let have = (self.buf.len() - header_len) as u64;
                    if have < payload_len {
                        return Ok(None);
                    }
                    let h = self.header.take().unwrap();
                    let event = self.complete_frame(h)?;
                    // 逐字节喂入时本帧恰好结束在缓冲末尾，直接清空即可。
                    self.buf.clear();
                    self.stage = Stage::Header;
                    return Ok(event);
                }
            }
        }
    }

    /// 一帧的帧头与全部 payload 已在 `self.buf` 中，去掩码并按状态机处理。
    fn complete_frame(&mut self, h: FrameHeader) -> WsResult<Option<Event>> {
        let header_len = h.header_len;
        // 把缓冲区取出来再去掩码，避免 payload 对 self.buf 的不可变借用与
        // 后续 &mut self 冲突。
        let mut data = mem::take(&mut self.buf);
        // 掩码密钥是帧头最后 4 字节，payload 紧跟帧头之后。
        unmask_payload(&mut data[header_len - 4..]);
        let payload: Vec<u8> = data[header_len..].to_vec();

        match h.opcode {
            // 控制帧可在任意时刻（含分片消息进行中）到达，不改动分片状态。
            Opcode::Ping => Ok(Some(Event::Ping(payload))),
            Opcode::Pong => Ok(Some(Event::Pong(payload))),
            Opcode::Close => {
                let parsed = parse_close_payload(&payload)?;
                self.closed = true;
                let (code, reason) = match parsed {
                    None => (None, None),
                    Some((c, r)) => (Some(c), r.map(str::to_string)),
                };
                Ok(Some(Event::Close { code, reason }))
            }
            Opcode::Text => self.start_data(true, h.fin, &payload),
            Opcode::Binary => self.start_data(false, h.fin, &payload),
            Opcode::Continuation => self.continue_data(h.fin, &payload),
            // parse_header 已拒绝保留操作码。
            Opcode::Reserved(op) => Err(WsError::UnknownOpcode(op)),
        }
    }

    /// 处理 text/binary 起始帧。
    fn start_data(
        &mut self,
        is_text: bool,
        fin: bool,
        payload: &[u8],
    ) -> WsResult<Option<Event>> {
        // §5.4：已有未结束的分片消息时，不允许再“开始”一条新消息。
        if !matches!(self.frag, Frag::Idle) {
            return Err(WsError::IllegalFragmentation);
        }

        if fin {
            // 单帧完整消息。
            if is_text {
                let mut utf8 = IncrementalUtf8::new();
                utf8.feed(payload).map_err(|_| WsError::InvalidUtf8)?;
                utf8.finish().map_err(|_| WsError::InvalidUtf8)?;
                // 再做一次整体校验作为纵深防御。
                let s = std::str::from_utf8(payload).map_err(|_| WsError::InvalidUtf8)?;
                Ok(Some(Event::Text(s.to_owned())))
            } else {
                self.check_message_size(payload.len())?;
                Ok(Some(Event::Binary(payload.to_vec())))
            }
        } else {
            // 分片起始：建立分片状态。
            self.check_message_size(payload.len())?;
            if is_text {
                let mut utf8 = IncrementalUtf8::new();
                // 起始分片允许结尾停在半个多字节字符上（只拒绝硬非法字节）。
                utf8.feed(payload).map_err(|_| WsError::InvalidUtf8)?;
                self.frag = Frag::Text(utf8);
            } else {
                self.frag = Frag::Binary;
            }
            self.message.extend_from_slice(payload);
            Ok(None)
        }
    }

    /// 处理 continuation 帧。
    fn continue_data(&mut self, fin: bool, payload: &[u8]) -> WsResult<Option<Event>> {
        // §5.4：没有进行中的分片消息却收到 continuation，非法。
        if matches!(self.frag, Frag::Idle) {
            return Err(WsError::IllegalFragmentation);
        }
        self.check_message_size(payload.len())?;

        let is_text = matches!(self.frag, Frag::Text(_));
        if is_text {
            let Frag::Text(utf8) = &mut self.frag else {
                unreachable!("checked above")
            };
            utf8.feed(payload).map_err(|_| WsError::InvalidUtf8)?;
            self.message.extend_from_slice(payload);
            if fin {
                utf8.finish().map_err(|_| WsError::InvalidUtf8)?;
                let bytes = mem::take(&mut self.message);
                self.frag = Frag::Idle;
                let s = String::from_utf8(bytes).map_err(|_| WsError::InvalidUtf8)?;
                Ok(Some(Event::Text(s)))
            } else {
                Ok(None)
            }
        } else {
            self.message.extend_from_slice(payload);
            if fin {
                let bytes = mem::take(&mut self.message);
                self.frag = Frag::Idle;
                Ok(Some(Event::Binary(bytes)))
            } else {
                Ok(None)
            }
        }
    }

    /// 追加 payload 前做整消息长度判定（1009）。
    fn check_message_size(&self, add: usize) -> WsResult<()> {
        let total = self.message.len().saturating_add(add);
        if total > self.limits.max_message_size {
            Err(WsError::MessageTooLarge(total))
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 测试辅助：真的**一个字节一个字节**喂入，收集全部事件。
    fn feed_byte_by_byte(bytes: &[u8]) -> WsResult<Vec<Event>> {
        let mut r = Reassembler::new_default();
        let mut events = Vec::new();
        for &b in bytes {
            if let Some(ev) = r.feed(b)? {
                events.push(ev);
            }
        }
        Ok(events)
    }

    /// 构造一个掩码后的客户端帧（fin/opcode 可调，便于造分片与控制帧）。
    fn frame(fin: bool, opcode: u8, payload: &[u8], key: [u8; 4]) -> Vec<u8> {
        let mut out = Vec::new();
        out.push(if fin { 0x80 | opcode } else { opcode });
        match payload.len() {
            n @ 0..=125 => out.push(0x80 | n as u8),
            n @ 126..=65535 => {
                out.push(0x80 | 126);
                out.extend_from_slice(&(n as u16).to_be_bytes());
            }
            n => {
                out.push(0x80 | 127);
                out.extend_from_slice(&(n as u64).to_be_bytes());
            }
        }
        out.extend_from_slice(&key);
        for (i, b) in payload.iter().enumerate() {
            out.push(b ^ key[i % 4]);
        }
        out
    }

    fn text(fin: bool, s: &str, key: [u8; 4]) -> Vec<u8> {
        frame(fin, 0x1, s.as_bytes(), key)
    }
    /// 文本帧但载荷可能不是合法 UTF-8（专门构造 1007 用例）。
    fn text_raw(fin: bool, b: &[u8], key: [u8; 4]) -> Vec<u8> {
        frame(fin, 0x1, b, key)
    }
    fn binary(fin: bool, b: &[u8], key: [u8; 4]) -> Vec<u8> {
        frame(fin, 0x2, b, key)
    }
    fn cont(fin: bool, s: &[u8], key: [u8; 4]) -> Vec<u8> {
        frame(fin, 0x0, s, key)
    }
    fn ping(payload: &[u8], key: [u8; 4]) -> Vec<u8> {
        frame(true, 0x9, payload, key)
    }
    fn pong(payload: &[u8], key: [u8; 4]) -> Vec<u8> {
        frame(true, 0xA, payload, key)
    }
    fn close(code: Option<u16>, reason: &str, key: [u8; 4]) -> Vec<u8> {
        let mut p = Vec::new();
        if let Some(c) = code {
            p.extend_from_slice(&c.to_be_bytes());
            p.extend_from_slice(reason.as_bytes());
        }
        frame(true, 0x8, &p, key)
    }

    #[test]
    fn single_unfragmented_text_and_binary() {
        let ev = feed_byte_by_byte(&text(true, "hello", [1; 4])).unwrap();
        assert_eq!(ev, vec![Event::Text("hello".to_string())]);

        let ev = feed_byte_by_byte(&binary(true, &[1, 2, 3], [9; 4])).unwrap();
        assert_eq!(ev, vec![Event::Binary(vec![1, 2, 3])]);
    }

    #[test]
    fn empty_text_and_empty_ping() {
        let ev = feed_byte_by_byte(&text(true, "", [7; 4])).unwrap();
        assert_eq!(ev, vec![Event::Text(String::new())]);

        let mut bytes = ping(&[], [5; 4]);
        bytes.extend_from_slice(&pong(&[], [5; 4]));
        let ev = feed_byte_by_byte(&bytes).unwrap();
        assert_eq!(ev, vec![Event::Ping(vec![]), Event::Pong(vec![])]);
    }

    #[test]
    fn fragmented_text_reassembles_across_three_frames() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text(false, "Hel", [1; 4]));
        bytes.extend_from_slice(&cont(false, b"lo ", [2; 4]));
        bytes.extend_from_slice(&cont(true, b"world", [3; 4]));
        let ev = feed_byte_by_byte(&bytes).unwrap();
        assert_eq!(ev, vec![Event::Text("Hello world".to_string())]);
    }

    #[test]
    fn ping_interleaved_between_fragments_is_emited_and_does_not_break_message() {
        // text! + ping（插在分片中间）+ pong + 续帧
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text(false, "Hel", [1; 4]));
        bytes.extend_from_slice(&ping(b"abc", [2; 4]));
        bytes.extend_from_slice(&pong(b"abc", [8; 4]));
        bytes.extend_from_slice(&cont(true, b"lo", [3; 4]));

        let ev = feed_byte_by_byte(&bytes).unwrap();
        assert_eq!(
            ev,
            vec![
                Event::Ping(b"abc".to_vec()),
                Event::Pong(b"abc".to_vec()),
                Event::Text("Hello".to_string()),
            ]
        );
    }

    #[test]
    fn illegal_continuation_without_start_is_1002() {
        // 一上来就是 continuation
        let err = feed_byte_by_byte(&cont(true, b"x", [1; 4])).unwrap_err();
        assert_eq!(err, WsError::IllegalFragmentation);
        assert_eq!(err.close_code().unwrap().0, 1002);
    }

    #[test]
    fn illegal_new_message_started_while_fragment_open_is_1002() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text(false, "ab", [1; 4]));
        // 前一条还没结束，又来一个 fin text 起始帧
        bytes.extend_from_slice(&text(true, "cd", [1; 4]));
        let err = feed_byte_by_byte(&bytes).unwrap_err();
        assert_eq!(err, WsError::IllegalFragmentation);
        assert_eq!(err.close_code().unwrap().0, 1002);

        // 非 fin 的起始帧同样非法
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&binary(false, &[1], [1; 4]));
        bytes.extend_from_slice(&text(false, "x", [1; 4]));
        assert_eq!(
            feed_byte_by_byte(&bytes).unwrap_err(),
            WsError::IllegalFragmentation
        );
    }

    #[test]
    fn half_a_utf8_character_split_across_frames_is_valid() {
        // “你” = E4 BD A0：起始帧含前 2 字节（半个字符），结束帧补齐。
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text_raw(false, &[0xE4, 0xBD], [1; 4]));
        bytes.extend_from_slice(&cont(true, &[0xA0], [1; 4]));
        let ev = feed_byte_by_byte(&bytes).unwrap();
        assert_eq!(ev, vec![Event::Text("你".to_string())]);
    }

    #[test]
    fn truncated_utf8_at_message_end_is_1007() {
        // fin 停在半个字符：E4 BD 之后永远等不到 A0
        let err = feed_byte_by_byte(&text_raw(true, &[0xE4, 0xBD], [1; 4])).unwrap_err();
        assert_eq!(err, WsError::InvalidUtf8);
        assert_eq!(err.close_code().unwrap().0, 1007);

        // 分片消息结束时仍差一个续字节
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text_raw(false, &[0xE4], [1; 4]));
        bytes.extend_from_slice(&cont(true, &[0xBD], [1; 4]));
        assert_eq!(
            feed_byte_by_byte(&bytes).unwrap_err(),
            WsError::InvalidUtf8
        );
    }

    #[test]
    fn invalid_utf8_byte_inside_continuation_is_1007() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&text(false, "a", [1; 4]));
        bytes.extend_from_slice(&cont(false, &[0xFF], [1; 4]));
        bytes.extend_from_slice(&cont(true, b"b", [1; 4]));
        assert_eq!(
            feed_byte_by_byte(&bytes).unwrap_err(),
            WsError::InvalidUtf8
        );
    }

    #[test]
    fn surrogate_and_overlong_sequences_rejected() {
        // U+D800 代理：ED A0 80
        assert_eq!(
            feed_byte_by_byte(&text_raw(true, &[0xED, 0xA0, 0x80], [1; 4])).unwrap_err(),
            WsError::InvalidUtf8
        );
        // 超长 NUL：C0 80
        assert_eq!(
            feed_byte_by_byte(&text_raw(true, &[0xC0, 0x80], [1; 4])).unwrap_err(),
            WsError::InvalidUtf8
        );
    }

    #[test]
    fn oversized_message_is_1009() {
        let limits = Limits {
            max_frame_payload: 1 << 20,
            max_message_size: 8,
        };
        let mut r = Reassembler::new(limits);
        let mut bytes = text(false, "1234", [1; 4]); // 4 字节，允许
        bytes.extend_from_slice(&cont(true, b"56789", [1; 4])); // 追加后 9 > 8
        let err = bytes
            .iter()
            .find_map(|&b| r.feed(b).transpose().and_then(Result::err))
            .unwrap_or_else(|| panic!("expected MessageTooLarge"));
        assert_eq!(err, WsError::MessageTooLarge(9));
        assert_eq!(err.close_code().unwrap().0, 1009);
    }

    #[test]
    fn oversized_single_frame_is_1009_at_header_layer() {
        let limits = Limits {
            max_frame_payload: 4,
            max_message_size: 1024,
        };
        let mut r = Reassembler::new(limits);
        let bytes = frame(true, 0x2, &[0u8; 5], [1; 4]);
        let err = bytes
            .iter()
            .find_map(|&b| r.feed(b).transpose().and_then(Result::err))
            .unwrap();
        assert_eq!(err, WsError::FrameTooLarge(5));
        assert_eq!(err.close_code().unwrap().0, 1009);
    }

    #[test]
    fn control_frame_126_bytes_is_protocol_error() {
        let big = vec![0u8; 126];
        let err = feed_byte_by_byte(&ping(&big, [1; 4])).unwrap_err();
        assert_eq!(err, WsError::ControlFrameTooLong(126));
        assert_eq!(err.close_code().unwrap().0, 1002);
    }

    #[test]
    fn unmasked_frame_is_1002() {
        // 直接构造：fin text，未掩码，len 0
        let err = feed_byte_by_byte(&[0x81, 0x00]).unwrap_err();
        assert_eq!(err, WsError::FrameNotMasked);
    }

    #[test]
    fn close_frame_events_and_close_codes_are_checked() {
        let ev = feed_byte_by_byte(&close(Some(1000), "bye", [1; 4])).unwrap();
        assert_eq!(
            ev,
            vec![Event::Close {
                code: Some(1000),
                reason: Some("bye".to_string())
            }]
        );

        // 空载荷 close：无码
        let ev = feed_byte_by_byte(&close(None, "", [1; 4])).unwrap();
        assert_eq!(ev, vec![Event::Close { code: None, reason: None }]);

        // 保留码 1005 出现在帧里 → 1002（通过 InvalidCloseFrame）
        let err = feed_byte_by_byte(&close(Some(1005), "", [1; 4])).unwrap_err();
        assert_eq!(err, WsError::InvalidCloseFrame);
        assert_eq!(err.close_code().unwrap().0, 1002);

        // 非 UTF-8 原因
        let mut p = Vec::new();
        p.extend_from_slice(&1000u16.to_be_bytes());
        p.push(0xFF);
        let err = feed_byte_by_byte(&frame(true, 0x8, &p, [1; 4])).unwrap_err();
        assert_eq!(err, WsError::InvalidCloseFrame);
    }

    #[test]
    fn any_frame_after_close_is_1002() {
        let mut bytes = close(Some(1000), "", [1; 4]);
        bytes.extend_from_slice(&text(true, "x", [1; 4]));
        let err = feed_byte_by_byte(&bytes).unwrap_err();
        assert_eq!(err, WsError::ConnectionClosed);
        assert_eq!(err.close_code().unwrap().0, 1002);
    }

    #[test]
    fn partial_header_then_rest_still_parses_byte_by_byte() {
        // 验证真逐字节：先只喂 1 个字节，长时间无事件，再补齐。
        let mut r = Reassembler::new_default();
        assert_eq!(r.feed(0x81).unwrap(), None);
        assert_eq!(r.feed(0x81).unwrap(), None); // len=1 + mask 标志
        for _ in 0..4 {
            assert_eq!(r.feed(0x00).unwrap(), None);
        }
        // 最后一个 payload 字节
        let ev = r.feed(b'A').unwrap().unwrap();
        assert_eq!(ev, Event::Text("A".to_string()));
    }

    #[test]
    fn binary_fragmented_with_ping_inside() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&binary(false, &[0xDE, 0xAD], [1; 4]));
        bytes.extend_from_slice(&ping(&[], [1; 4]));
        bytes.extend_from_slice(&cont(true, &[0xBE, 0xEF], [1; 4]));
        let ev = feed_byte_by_byte(&bytes).unwrap();
        assert_eq!(
            ev,
            vec![
                Event::Ping(vec![]),
                Event::Binary(vec![0xDE, 0xAD, 0xBE, 0xEF])
            ]
        );
    }

    #[test]
    fn unsolicited_pong_when_idle_is_allowed() {
        // §5.5.3：响应未请求的 pong 可以被静默接受
        let ev = feed_byte_by_byte(&pong(b"x", [1; 4])).unwrap();
        assert_eq!(ev, vec![Event::Pong(b"x".to_vec())]);
    }
}
