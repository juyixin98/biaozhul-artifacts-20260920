//! 消息重组：在帧解析器之上实现 RFC 6455 §5.4 的分片序列状态机、
//! §5.5 的控制帧插入规则、§5.6 的关闭帧校验，以及**跨分片** UTF-8 验证。
//!
//! 合法序列示例（`<...>` 表示一个消息）：
//! ```text
//! [text FIN=0] [ping FIN=1] [cont FIN=0] [cont FIN=1]
//!            ^ 控制帧可插入分片之间，但不能打断单个帧
//! ```

use crate::error::{CloseCode, Error};
use crate::frame::{Frame, Opcode};
use crate::utf8::Utf8Validator;

/// 一条重组完成的应用层消息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub is_text: bool,
    pub data: Vec<u8>,
}

/// 重组器对单帧的处理结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// 一条完整消息（text/binary，可能由多个分片组成）。
    Message(Message),
    Ping(Vec<u8>),
    Pong(Vec<u8>),
    /// 关闭请求；`code` 为关闭码（无正文时为 None，等价于 1005，按 1000 回），
    /// `raw_body` 为关闭帧原始正文（用于原样回显）。
    Close {
        code: Option<u16>,
        raw_body: Vec<u8>,
    },
    /// 分片进行中但尚未构成完整消息（无应用层数据，调用方可忽略）。
    StreamProgress,
}

/// 当前分片序列状态。
#[derive(Debug, Clone)]
enum FragState {
    /// 没有进行中的分片消息。
    Idle,
    /// 正在收集分片，记录起始帧类型与已累积数据。
    Streaming {
        is_text: bool,
        buf: Vec<u8>,
        utf8: Utf8Validator,
    },
}

/// 消息重组器。
pub struct Assembler {
    max_message: usize,
    state: FragState,
}

impl Assembler {
    pub fn new(max_message: usize) -> Self {
        Assembler {
            max_message,
            state: FragState::Idle,
        }
    }

    /// 处理一帧，返回应用层事件。
    ///
    /// 注意：控制帧可能在分片序列中途出现，调用方必须先处理返回的事件
    /// （如 Ping→Pong），重组器内部状态不受其影响。
    pub fn handle(&mut self, frame: Frame) -> Result<Event, Error> {
        match frame.opcode {
            Opcode::Ping => Ok(Event::Ping(frame.payload)),
            Opcode::Pong => Ok(Event::Pong(frame.payload)),
            Opcode::Close => self.handle_close(frame.payload),

            Opcode::Text | Opcode::Binary => {
                if let FragState::Streaming { .. } = self.state {
                    // 上一个分片消息还没收到 FIN，又来一个新起始帧 → 协议错误
                    return Err(Error::NestedMessageStart);
                }
                let is_text = frame.opcode == Opcode::Text;
                self.start_message(is_text, frame.fin, frame.payload)
            }

            Opcode::Continuation => {
                let FragState::Streaming { is_text, buf, utf8 } =
                    std::mem::replace(&mut self.state, FragState::Idle)
                else {
                    return Err(Error::UnexpectedContinuation);
                };
                self.append_continuation(is_text, buf, utf8, frame.fin, frame.payload)
            }
        }
    }

    fn start_message(
        &mut self,
        is_text: bool,
        fin: bool,
        payload: Vec<u8>,
    ) -> Result<Event, Error> {
        if payload.len() > self.max_message {
            return Err(Error::MessageTooLarge);
        }
        if fin {
            if is_text {
                validate_complete_text(&payload)?;
            }
            return Ok(Event::Message(Message {
                is_text,
                data: payload,
            }));
        }
        // 起始分片 FIN=0：建立流状态，并立即验证本分片携带的字节
        let mut utf8 = Utf8Validator::new();
        if is_text {
            utf8.feed_slice(&payload)?;
        }
        self.state = FragState::Streaming {
            is_text,
            buf: payload,
            utf8,
        };
        Ok(Event::StreamProgress) // 见下方：未完成，不产生消息事件
    }

    fn append_continuation(
        &mut self,
        is_text: bool,
        mut buf: Vec<u8>,
        mut utf8: Utf8Validator,
        fin: bool,
        payload: Vec<u8>,
    ) -> Result<Event, Error> {
        if buf.len() + payload.len() > self.max_message {
            return Err(Error::MessageTooLarge);
        }
        if is_text {
            // 跨分片 UTF-8 验证：分片边界允许落在字符中间
            utf8.feed_slice(&payload)?;
        }
        buf.extend_from_slice(&payload);

        if fin {
            if is_text {
                utf8.finish()?; // 消息结束时不允许停在半个字符
            }
            Ok(Event::Message(Message { is_text, data: buf }))
        } else {
            self.state = FragState::Streaming { is_text, buf, utf8 };
            Ok(Event::StreamProgress)
        }
    }

    fn handle_close(&mut self, body: Vec<u8>) -> Result<Event, Error> {
        // §5.5.1：若还有未结束的分片消息，连接状态为异常；
        // RFC 允许先发 Close，本实现返回事件但标记该情况由调用方决定。
        let mid_stream = matches!(self.state, FragState::Streaming { .. });

        let code = match body.len() {
            0 => None, // 没有状态码，视为空关闭
            1 => return Err(Error::InvalidCloseFrame),
            _ => {
                let code = u16::from_be_bytes([body[0], body[1]]);
                if !CloseCode::is_allowed_on_wire(code) {
                    return Err(Error::InvalidCloseFrame);
                }
                // 关闭原因必须是合法 UTF-8（§5.5.1）
                validate_complete_text(&body[2..])?;
                Some(code)
            }
        };

        if mid_stream {
            // 仍向调用方返回 Close 事件（需要回 Close），但带上异常标记。
            // 为保持 Event 枚举简洁，这里返回错误让服务端按协议错误关闭。
            return Err(Error::ClosingDuringFragment);
        }
        Ok(Event::Close {
            code,
            raw_body: body,
        })
    }
}

fn validate_complete_text(bytes: &[u8]) -> Result<(), Error> {
    let mut v = Utf8Validator::new();
    v.feed_slice(bytes)?;
    v.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(fin: bool, op: Opcode, payload: &[u8]) -> Frame {
        Frame::new(fin, op, payload.to_vec())
    }

    #[test]
    fn single_unfragmented_text() {
        let mut a = Assembler::new(1024);
        let ev = a.handle(f(true, Opcode::Text, b"hi")).unwrap();
        assert_eq!(
            ev,
            Event::Message(Message {
                is_text: true,
                data: b"hi".to_vec()
            })
        );
    }

    #[test]
    fn fragmented_text_with_embedded_ping() {
        let mut a = Assembler::new(1024);
        // "中" = E4 B8 AD，被切成 E4 | B8 AD 两部分，中间插一个 ping
        assert_eq!(
            a.handle(f(false, Opcode::Text, &[0xE4])).unwrap(),
            Event::StreamProgress
        );
        let ping = a.handle(f(true, Opcode::Ping, b"p")).unwrap();
        assert_eq!(ping, Event::Ping(b"p".to_vec()));
        assert_eq!(
            a.handle(f(false, Opcode::Continuation, &[0xB8])).unwrap(),
            Event::StreamProgress
        );
        let msg = a.handle(f(true, Opcode::Continuation, &[0xAD])).unwrap();
        assert_eq!(
            msg,
            Event::Message(Message {
                is_text: true,
                data: vec![0xE4, 0xB8, 0xAD]
            })
        );
    }

    #[test]
    fn rejects_continuation_without_start() {
        let mut a = Assembler::new(1024);
        assert_eq!(
            a.handle(f(true, Opcode::Continuation, b"x")).unwrap_err(),
            Error::UnexpectedContinuation
        );
    }

    #[test]
    fn rejects_nested_message_start() {
        let mut a = Assembler::new(1024);
        a.handle(f(false, Opcode::Text, b"a")).unwrap();
        assert_eq!(
            a.handle(f(false, Opcode::Binary, b"b")).unwrap_err(),
            Error::NestedMessageStart
        );
    }

    #[test]
    fn rejects_half_utf8_at_message_end() {
        let mut a = Assembler::new(1024);
        a.handle(f(false, Opcode::Text, &[0xE4, 0xB8])).unwrap();
        // 结束分片缺少最后一个字节 → 半个字符
        assert_eq!(
            a.handle(f(true, Opcode::Continuation, &[])).unwrap_err(),
            Error::InvalidUtf8
        );
    }

    #[test]
    fn rejects_bad_utf8_inside_first_fragment() {
        let mut a = Assembler::new(1024);
        // 代理区编码 ED A0 80
        assert_eq!(
            a.handle(f(false, Opcode::Text, &[0xED, 0xA0, 0x80]))
                .unwrap_err(),
            Error::InvalidUtf8
        );
    }

    #[test]
    fn rejects_bad_utf8_spanning_fragments() {
        let mut a = Assembler::new(1024);
        a.handle(f(false, Opcode::Text, &[0xED])).unwrap(); // 此时还无法判定
        assert_eq!(
            a.handle(f(true, Opcode::Continuation, &[0xA0, 0x80]))
                .unwrap_err(),
            Error::InvalidUtf8
        );
    }

    #[test]
    fn rejects_message_too_big_single_frame() {
        let mut a = Assembler::new(4);
        assert_eq!(
            a.handle(f(true, Opcode::Binary, b"hello")).unwrap_err(),
            Error::MessageTooLarge
        );
    }

    #[test]
    fn rejects_message_too_big_accumulated() {
        let mut a = Assembler::new(4);
        a.handle(f(false, Opcode::Binary, b"ab")).unwrap();
        a.handle(f(false, Opcode::Continuation, b"cd")).unwrap();
        assert_eq!(
            a.handle(f(true, Opcode::Continuation, b"x")).unwrap_err(),
            Error::MessageTooLarge
        );
    }

    #[test]
    fn valid_close_codes() {
        let mut a = Assembler::new(1024);
        let body = 1000u16.to_be_bytes();
        assert_eq!(
            a.handle(f(true, Opcode::Close, &body)).unwrap(),
            Event::Close {
                code: Some(1000),
                raw_body: body.to_vec()
            }
        );

        let mut a2 = Assembler::new(1024);
        let mut body = 1001u16.to_be_bytes().to_vec();
        body.extend_from_slice(b"bye");
        let ev = a2.handle(f(true, Opcode::Close, &body)).unwrap();
        assert_eq!(
            ev,
            Event::Close {
                code: Some(1001),
                raw_body: body
            }
        );
    }

    #[test]
    fn empty_close_body_is_ok() {
        let mut a = Assembler::new(1024);
        assert_eq!(
            a.handle(f(true, Opcode::Close, b"")).unwrap(),
            Event::Close {
                code: None,
                raw_body: vec![]
            }
        );
    }

    #[test]
    fn rejects_close_body_len_one() {
        let mut a = Assembler::new(1024);
        assert_eq!(
            a.handle(f(true, Opcode::Close, &[0x03])).unwrap_err(),
            Error::InvalidCloseFrame
        );
    }

    #[test]
    fn rejects_unknown_close_code() {
        // 1004/1005/1006/1015 都禁止出现在帧中
        for bad in [1004u16, 1005, 1006, 1015] {
            let mut a = Assembler::new(1024);
            let body = bad.to_be_bytes();
            assert_eq!(
                a.handle(f(true, Opcode::Close, &body)).unwrap_err(),
                Error::InvalidCloseFrame,
                "code {bad} should be rejected"
            );
        }
    }

    #[test]
    fn rejects_close_with_non_utf8_reason() {
        let mut a = Assembler::new(1024);
        let mut body = 1000u16.to_be_bytes().to_vec();
        body.push(0xFF);
        assert_eq!(
            a.handle(f(true, Opcode::Close, &body)).unwrap_err(),
            Error::InvalidUtf8
        );
    }

    #[test]
    fn rejects_close_during_open_fragment() {
        let mut a = Assembler::new(1024);
        a.handle(f(false, Opcode::Text, b"a")).unwrap();
        assert_eq!(
            a.handle(f(true, Opcode::Close, b"")).unwrap_err(),
            Error::ClosingDuringFragment
        );
    }
}
