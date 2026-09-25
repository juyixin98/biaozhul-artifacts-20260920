//! 增量（流式）UTF-8 校验器。
//!
//! 与 `std::str::from_utf8` 的区别：本校验器可以**跨分片**验证 —
//! 一个多字节 UTF-8 字符允许被切分到两个不同的 WebSocket 分片里，
//! 校验器会记住"还差几个 continuation 字节"以及该字符的码点下限，
//! 从而在字节到达时尽早发现非法序列（如代理字节、过长编码）。
//!
//! 规则（RFC 3629 / Unicode）：
//! - 1 字节：00-7F
//! - 2 字节：C2-DF 80-BF（C0/C1 是过长编码，非法）
//! - 3 字节：E0 A0-BF / E1-EC 80-BF / ED 80-9F / EE-EF 80-BF
//!   （ED A0-BF 是 UTF-16 代理区，非法）
//! - 4 字节：F0 90-BF / F1-F3 80-BF / F4 80-8F（> U+10FFFF 非法）

use crate::error::Error;

/// 增量 UTF-8 校验器。每验证完一个完整字符自动复位，
/// 只保留"半个字符"的上下文。
#[derive(Debug, Default, Clone)]
pub struct Utf8Validator {
    /// 已经收到的 continuation 前缀字节（不含 lead 字节本身）。
    pending: [u8; 3],
    /// 已收到的字节数（含 lead）。
    have: u8,
    /// 该字符总共需要的字节数（含 lead）。
    need: u8,
    /// 该字符码点的最小合法值（用于拒绝过长编码 / 代理区 / 超范围）。
    min_codepoint: u32,
    /// 该字符码点的最大合法值。
    max_codepoint: u32,
    /// 当前累积的码点。
    codepoint: u32,
}

impl Utf8Validator {
    pub fn new() -> Self {
        Self::default()
    }

    /// 当前是否处于"半个字符"状态（消息结束时不允许）。
    pub fn is_mid_character(&self) -> bool {
        self.have > 0
    }

    /// 喂入一个字节。返回 `Ok(())` 表示目前为止合法；
    /// 返回 `Err(Error::InvalidUtf8)` 表示序列非法。
    pub fn feed(&mut self, byte: u8) -> Result<(), Error> {
        if self.have == 0 {
            self.start_character(byte)
        } else {
            self.continue_character(byte)
        }
    }

    /// 喂入一段字节切片（逐字节调用 [`feed`](Self::feed)）。
    pub fn feed_slice(&mut self, bytes: &[u8]) -> Result<(), Error> {
        for &b in bytes {
            self.feed(b)?;
        }
        Ok(())
    }

    /// 消息结束时调用：若仍处于半个字符状态则报错。
    pub fn finish(&self) -> Result<(), Error> {
        if self.is_mid_character() {
            Err(Error::InvalidUtf8)
        } else {
            Ok(())
        }
    }

    fn start_character(&mut self, byte: u8) -> Result<(), Error> {
        match byte {
            0x00..=0x7F => Ok(()), // ASCII，单字节完成
            0xC2..=0xDF => {
                self.begin(2, 0x80, 0x7FF, (byte as u32) & 0x1F);
                Ok(())
            }
            0xE0..=0xEF => {
                self.begin(3, 0x800, 0xFFFF, (byte as u32) & 0x0F);
                Ok(())
            }
            0xF0..=0xF4 => {
                self.begin(4, 0x10000, 0x10FFFF, (byte as u32) & 0x07);
                Ok(())
            }
            // 0x80-0xBF 裸 continuation、0xC0/0xC1 过长编码、0xF5-0xFF 超范围
            _ => Err(Error::InvalidUtf8),
        }
    }

    fn begin(&mut self, need: u8, min: u32, max: u32, seed: u32) {
        self.have = 1;
        self.need = need;
        self.min_codepoint = min;
        self.max_codepoint = max;
        self.codepoint = seed;
    }

    fn continue_character(&mut self, byte: u8) -> Result<(), Error> {
        if !(0x80..=0xBF).contains(&byte) {
            return Err(Error::InvalidUtf8);
        }
        self.codepoint = (self.codepoint << 6) | ((byte as u32) & 0x3F);
        self.pending[(self.have - 1) as usize] = byte;
        self.have += 1;
        if self.have == self.need {
            // 完整字符：检查码点范围（过长编码 / 代理区 / > U+10FFFF）
            if self.codepoint < self.min_codepoint || self.codepoint > self.max_codepoint {
                self.reset();
                return Err(Error::InvalidUtf8);
            }
            // UTF-16 代理区 D800-DFFF 不允许出现在 UTF-8 中
            if (0xD800..=0xDFFF).contains(&self.codepoint) {
                self.reset();
                return Err(Error::InvalidUtf8);
            }
            self.reset();
        }
        Ok(())
    }

    fn reset(&mut self) {
        self.have = 0;
        self.need = 0;
        self.codepoint = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn validate(bytes: &[u8]) -> Result<(), Error> {
        let mut v = Utf8Validator::new();
        v.feed_slice(bytes)?;
        v.finish()
    }

    #[test]
    fn accepts_ascii_and_multibyte() {
        assert!(validate(b"hello").is_ok());
        assert!(validate("你好，世界".as_bytes()).is_ok());
        assert!(validate("🦀 rust".as_bytes()).is_ok());
    }

    #[test]
    fn accepts_split_multibyte_character() {
        // "中" = E4 B8 AD，逐字节喂入模拟跨分片
        let mut v = Utf8Validator::new();
        assert!(v.feed(0xE4).is_ok());
        assert!(v.is_mid_character());
        assert!(v.feed(0xB8).is_ok());
        assert!(v.feed(0xAD).is_ok());
        assert!(!v.is_mid_character());
        assert!(v.finish().is_ok());
    }

    #[test]
    fn rejects_dangling_lead_at_finish() {
        let mut v = Utf8Validator::new();
        v.feed(0xE4).unwrap();
        v.feed(0xB8).unwrap();
        assert_eq!(v.finish(), Err(Error::InvalidUtf8));
    }

    #[test]
    fn rejects_bare_continuation() {
        assert_eq!(validate(&[0x80]), Err(Error::InvalidUtf8));
    }

    #[test]
    fn rejects_overlong_encodings() {
        // C0 80 / C1 BF 是 NUL / 0x7F 的过长编码
        assert_eq!(validate(&[0xC0, 0x80]), Err(Error::InvalidUtf8));
        assert_eq!(validate(&[0xC1, 0xBF]), Err(Error::InvalidUtf8));
        // E0 80 80 是 U+0000 的过长编码
        assert_eq!(validate(&[0xE0, 0x80, 0x80]), Err(Error::InvalidUtf8));
        // F0 80 80 80 过长
        assert_eq!(validate(&[0xF0, 0x80, 0x80, 0x80]), Err(Error::InvalidUtf8));
    }

    #[test]
    fn rejects_surrogates() {
        // U+D800 = ED A0 80
        assert_eq!(validate(&[0xED, 0xA0, 0x80]), Err(Error::InvalidUtf8));
        // U+DFFF = ED BF BF
        assert_eq!(validate(&[0xED, 0xBF, 0xBF]), Err(Error::InvalidUtf8));
    }

    #[test]
    fn rejects_out_of_range() {
        // F4 90 80 80 = U+110000 > U+10FFFF
        assert_eq!(validate(&[0xF4, 0x90, 0x80, 0x80]), Err(Error::InvalidUtf8));
        assert_eq!(validate(&[0xF5, 0x80, 0x80, 0x80]), Err(Error::InvalidUtf8));
    }

    #[test]
    fn rejects_truncated_sequence_followed_by_ascii() {
        // E4 后面跟 ASCII 'a'：第二个字节不是 continuation
        assert_eq!(validate(&[0xE4, b'a']), Err(Error::InvalidUtf8));
    }
}
