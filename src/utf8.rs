//! 可增量续写的 UTF-8 校验器（RFC 3629 合法序列，拒绝超长编码与代理区）。
//!
//! WebSocket 文本消息可能被拆成任意多个分片，**每个分片单独看不一定是合法 UTF-8**
//! （RFC 6455 §5.6 要求按“重组后的完整消息”校验）。本模块保存一个多字节字符
//! 尚未到齐的中间状态，喂入新字节时续写，从而支持「半个 UTF-8 字符」跨帧边界。
//!
//! 采用显式状态机而非依赖 `str::from_utf8`：后者只能整体校验，无法表达中间状态。

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    /// 字符边界上。
    Boundary,
    /// 已收到 3 字节序列的首字节 0xE0，续字节受限（0xA0–0xBF）。
    E0,
    /// 已收到 3 字节序列的普通首字节 0xE1–0xEC/0xEE/0xEF（续字节 0x80–0xBF）。
    Ex,
    /// 已收到首字节 0xED，续字节受限（0x80–0x9F，排除 U+D800–U+DFFF 代理区）。
    ED,
    /// 3 字节序列已收到 1 个续字节，还差 1 个（2 字节序列首字节后也进入此状态）。
    Tail1,
    /// 已收到 4 字节序列首字节 0xF0，续字节受限（0x90–0xBF，排除超长）。
    F0,
    /// 已收到 4 字节序列首字节 0xF4，续字节受限（0x80–0x8F，排除 > U+10FFFF）。
    F4,
    /// 已收到普通 4 字节首字节 0xF1–0xF3（续字节 0x80–0xBF）。
    Fx,
    /// 4 字节序列已收到 1 个续字节，还差 2 个。
    Need2Of4,
    /// 4 字节序列已收到 2 个续字节，还差 1 个。
    Need1Of4,
}

/// 增量 UTF-8 校验器。每个新 text 分片消息开始时新建一个并贯穿其全部分片。
#[derive(Debug, Clone)]
pub struct IncrementalUtf8 {
    state: State,
}

impl Default for IncrementalUtf8 {
    fn default() -> Self {
        Self::new()
    }
}

/// 增量 UTF-8 校验失败标记（无额外信息：任何非法字节的处理方式都相同——1007）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidUtf8;

/// 增量 UTF-8 校验结果。
pub type Utf8Result = Result<(), InvalidUtf8>;

impl IncrementalUtf8 {
    pub fn new() -> Self {
        Self {
            state: State::Boundary,
        }
    }

    /// 喂入下一段字节。任何非法字节立即返回 [`InvalidUtf8`]；
    /// 出错后状态无意义（调用方应直接以 1007 关闭连接）。
    pub fn feed(&mut self, bytes: &[u8]) -> Utf8Result {
        use State::*;
        for &b in bytes {
            self.state = match (self.state, b) {
                (Boundary, 0x00..=0x7F) => Boundary,
                (Boundary, 0xC2..=0xDF) => Tail1,
                (Boundary, 0xE0) => E0,
                (Boundary, 0xE1..=0xEC) | (Boundary, 0xEE..=0xEF) => Ex,
                (Boundary, 0xED) => ED,
                (Boundary, 0xF0) => F0,
                (Boundary, 0xF1..=0xF3) => Fx,
                (Boundary, 0xF4) => F4,

                (E0, 0xA0..=0xBF) => Tail1,
                (Ex, 0x80..=0xBF) => Tail1,
                (ED, 0x80..=0x9F) => Tail1,
                (Tail1, 0x80..=0xBF) => Boundary,

                (F0, 0x90..=0xBF) => Need2Of4,
                (Fx, 0x80..=0xBF) => Need2Of4,
                (F4, 0x80..=0x8F) => Need2Of4,
                (Need2Of4, 0x80..=0xBF) => Need1Of4,
                (Need1Of4, 0x80..=0xBF) => Boundary,

                // 其余一切（非法首字节、非法续字节、越界、超长编码）均拒绝。
                _ => return Err(InvalidUtf8),
            };
        }
        Ok(())
    }

    /// 消息结束（fin）时调用：恰好停在字符边界上才合法，
    /// 否则说明末尾是「半个字符」。
    pub fn finish(&self) -> Utf8Result {
        if self.state == State::Boundary {
            Ok(())
        } else {
            Err(InvalidUtf8)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(splits: &[&[u8]], whole: &[u8]) -> bool {
        let mut v = IncrementalUtf8::new();
        for s in splits {
            if v.feed(s).is_err() {
                return false;
            }
        }
        let ok_split = v.finish().is_ok();
        let mut w = IncrementalUtf8::new();
        let ok_whole = w.feed(whole).is_ok() && w.finish().is_ok();
        ok_split == ok_whole
    }

    #[test]
    fn ascii_and_common_multibyte() {
        let mut v = IncrementalUtf8::new();
        v.feed(b"hello").unwrap();
        v.finish().unwrap();
        // “你好” = E4 BD A0 E5 A5 BD
        let mut v = IncrementalUtf8::new();
        v.feed(&[0xE4, 0xBD, 0xA0, 0xE5, 0xA5, 0xBD]).unwrap();
        v.finish().unwrap();
        // U+1F600 😀 = F0 9F 98 80
        let mut v = IncrementalUtf8::new();
        v.feed(&[0xF0, 0x9F, 0x98, 0x80]).unwrap();
        v.finish().unwrap();
    }

    #[test]
    fn half_a_character_split_across_chunks_is_held_open() {
        // 单个 “你”（E4 BD A0）被切成 1+2
        let mut v = IncrementalUtf8::new();
        v.feed(&[0xE4]).unwrap(); // 此时 finish 必须失败（半个字符）
        assert!(v.finish().is_err());
        v.feed(&[0xBD, 0xA0]).unwrap();
        v.finish().unwrap();

        // 3 字节字符按 2+1 切
        assert!(check(&[&[0xE4u8, 0xBD][..], &[0xA0][..]], &[0xE4, 0xBD, 0xA0]));
        // 4 字节字符切成 1+1+2
        assert!(check(
            &[&[0xF0u8][..], &[0x9F][..], &[0x98, 0x80][..]],
            &[0xF0, 0x9F, 0x98, 0x80]
        ));
    }

    #[test]
    fn rejects_overlong_and_surrogates_and_out_of_range() {
        // 超长编码 '/' 用 2 字节：C0 AF
        assert!(IncrementalUtf8::new().feed(&[0xC0, 0xAF]).is_err());
        // 超长 3 字节 E0 80 80
        assert!(IncrementalUtf8::new()
            .feed(&[0xE0, 0x80, 0x80])
            .is_err());
        // 代理区 U+D800：ED A0 80
        assert!(IncrementalUtf8::new()
            .feed(&[0xED, 0xA0, 0x80])
            .is_err());
        // 超过 U+10FFFF：F4 90 80 80
        assert!(IncrementalUtf8::new()
            .feed(&[0xF4, 0x90, 0x80, 0x80])
            .is_err());
        // 孤立续字节
        assert!(IncrementalUtf8::new().feed(&[0x80]).is_err());
        // 5 字节序列
        assert!(IncrementalUtf8::new()
            .feed(&[0xF8, 0x80, 0x80, 0x80, 0x80])
            .is_err());
    }
}
