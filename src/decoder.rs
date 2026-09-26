//! 流式 LZ77 解码器。
//!
//! 安全不变量：
//! - 回距必须满足 `1 <= dist <= min(window, 已输出字节数)`，即引用不得越过已有输出；
//! - 支持重叠复制（len > dist 时逐字节拷贝）；
//! - 每次发射前检查输出预算，超限即报 [`Error::OutputLimitExceeded`]；
//! - 内存有界：仅保留 window 字节历史 + 当前 token（≤ 130 字节）。

use std::collections::VecDeque;

use crate::error::{Error, Result};
use crate::format::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    Header,
    Control,
    Payload,
}

/// 流式解码器。通过 [`Decoder::feed`] 分块喂入，[`Decoder::finish`] 校验完整性。
pub struct Decoder {
    max_output: u64,
    emitted: u64,
    /// 头部声明的窗口；头部解析完成前为 0。
    window: usize,
    hist: VecDeque<u8>,
    state: State,
    hdr: Vec<u8>,
    ctrl: u8,
    payload: Vec<u8>,
    need: usize,
    tokens: u64,
}

impl Decoder {
    /// `max_output`：解码输出预算（字节），超限报错，防止压缩炸弹。
    pub fn new(max_output: u64) -> Self {
        Decoder {
            max_output,
            emitted: 0,
            window: 0,
            hist: VecDeque::new(),
            state: State::Header,
            hdr: Vec::with_capacity(HEADER_LEN),
            ctrl: 0,
            payload: Vec::with_capacity(MAX_LITERAL_RUN),
            need: 0,
            tokens: 0,
        }
    }

    /// 喂入一块压缩数据，解码结果追加到 `out`。
    pub fn feed(&mut self, data: &[u8], out: &mut Vec<u8>) -> Result<()> {
        for &b in data {
            match self.state {
                State::Header => {
                    self.hdr.push(b);
                    if self.hdr.len() == HEADER_LEN {
                        self.parse_header()?;
                        self.state = State::Control;
                    }
                }
                State::Control => {
                    self.ctrl = b;
                    self.payload.clear();
                    self.need = if b & 0x80 != 0 {
                        2 // 匹配 token：2 字节回距
                    } else {
                        (b & 0x7f) as usize + 1 // 字面量 token：len 个原始字节
                    };
                    self.state = State::Payload;
                }
                State::Payload => {
                    self.payload.push(b);
                    if self.payload.len() == self.need {
                        self.emit(out)?;
                        self.state = State::Control;
                    }
                }
            }
        }
        Ok(())
    }

    /// 校验流完整结束（不在 token 中途）。返回 token 总数。
    pub fn finish(self) -> Result<u64> {
        if self.state != State::Control {
            return Err(Error::Truncated);
        }
        Ok(self.tokens)
    }

    fn parse_header(&mut self) -> Result<()> {
        if self.hdr[0..4] != MAGIC[..] {
            return Err(Error::InvalidMagic);
        }
        if self.hdr[4] != VERSION {
            return Err(Error::UnsupportedVersion(self.hdr[4]));
        }
        if self.hdr[5] != 0 {
            return Err(Error::InvalidHeader("flags byte must be 0"));
        }
        let window = u16::from_le_bytes([self.hdr[6], self.hdr[7]]) as usize;
        if window == 0 {
            return Err(Error::InvalidHeader("window size must be >= 1"));
        }
        let max_match = u16::from_le_bytes([self.hdr[8], self.hdr[9]]) as usize;
        if max_match != MAX_MATCH {
            return Err(Error::InvalidHeader(
                "max_match does not match this implementation",
            ));
        }
        if self.hdr[10..12] != [0, 0] {
            return Err(Error::InvalidHeader("reserved bytes must be 0"));
        }
        self.window = window;
        Ok(())
    }

    fn emit(&mut self, out: &mut Vec<u8>) -> Result<()> {
        if self.ctrl & 0x80 != 0 {
            let len = (self.ctrl & 0x7f) as usize + MIN_MATCH;
            let dist = u16::from_le_bytes([self.payload[0], self.payload[1]]) as usize;
            if dist == 0 {
                return Err(Error::DistanceZero);
            }
            if dist > self.window {
                return Err(Error::DistanceBeyondWindow {
                    distance: dist,
                    window: self.window,
                });
            }
            if dist as u64 > self.emitted {
                return Err(Error::DistanceBeforeOutput {
                    distance: dist,
                    emitted: self.emitted,
                });
            }
            self.reserve(len)?;
            // 逐字节拷贝：len > dist 时自然形成重叠复制（如 "ab" + (dist=2,len=10)）。
            for _ in 0..len {
                let b = self.hist[self.hist.len() - dist];
                self.push_byte(b, out);
            }
        } else {
            let len = self.need;
            self.reserve(len)?;
            // payload 在本循环内不会被 push_byte 修改，先拷贝再逐字节入历史。
            let payload = std::mem::take(&mut self.payload);
            for &b in &payload {
                self.push_byte(b, out);
            }
            self.payload = payload;
        }
        self.tokens += 1;
        Ok(())
    }

    fn reserve(&self, n: usize) -> Result<()> {
        if self.emitted + n as u64 > self.max_output {
            return Err(Error::OutputLimitExceeded {
                limit: self.max_output,
            });
        }
        Ok(())
    }

    fn push_byte(&mut self, b: u8, out: &mut Vec<u8>) {
        out.push(b);
        self.hist.push_back(b);
        if self.hist.len() > self.window {
            self.hist.pop_front();
        }
        self.emitted += 1;
    }
}
