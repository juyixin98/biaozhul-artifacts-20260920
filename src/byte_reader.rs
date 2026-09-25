//! # 增量字节解析库（手写，无三方依赖）
//!
//! 本模块是整个项目的解析基础设施，**不依赖任何现成协议解析库**。
//!
//! ## 三个明确能力
//!
//! 1. **子集（subset）**：[`Reader::take_subset`] 从当前位置切出一个
//!    只能读取指定字节数的子解析器；[`Reader::limited`] 给整段缓冲区
//!    施加逻辑长度上限。子解析器无法越过边界读取。
//! 2. **长度上限**：读取前显式校验剩余长度。越过“调用方给定的逻辑
//!    上限”报 [`ParseError::LimitExceeded`]；越过“底层缓冲区物理
//!    结尾”报 [`ParseError::Incomplete`]（喂更多字节后可重试）。
//! 3. **错误类型**：所有解析失败都是显式分类的 [`ParseError`]，
//!    不会 panic、不会越界。
//!
//! ## 增量喂入
//!
//! [`FeedBuffer`] 维护“已喂入但尚未消费”的字节流，配合
//! [`FeedBuffer::try_parse`] 实现随到随解析：数据不足时返回
//! `Ok(None)` 且保留现场，之后 [`FeedBuffer::feed`] 追加字节再试。
//!
//! ```
//! use ipfrag::byte_reader::{FeedBuffer, ParseError, Reader};
//!
//! let mut buf = FeedBuffer::new();
//! buf.feed(&[0x00, 0x01]);
//! // 想读一个 u32，但只有 2 字节
//! let r: Option<u32> = buf
//!     .try_parse(|r| Ok(r.read_u32_be()?))
//!     .unwrap();
//! assert!(r.is_none());
//!
//! buf.feed(&[0x00, 0x02]);
//! let v = buf
//!     .try_parse(|r| r.read_u32_be())
//!     .unwrap()
//!     .unwrap();
//! assert_eq!(v, 0x0001_0002);
//! assert_eq!(buf.unconsumed_len(), 0);
//! ```

use std::fmt;

/// 字节解析错误。全部变体都实现了 [`std::error::Error`]。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// 字节不足（流尚未到齐）。`needed` 为至少还缺的字节数，
    /// `None` 表示调用方未给出确定长度；`available` 为当前可用字节数。
    Incomplete {
        needed: Option<usize>,
        available: usize,
    },
    /// 读取超过了调用方显式施加的长度上限（子集边界 / limit）。
    LimitExceeded {
        /// 本次请求读取的字节数
        requested: usize,
        /// 逻辑边界内剩余字节数
        remaining: usize,
    },
    /// 字段取值不合法。
    InvalidInput { message: String },
    /// 调用 [`Reader::finish`] 时帧内仍有未消费字节。
    TrailingBytes { remaining: usize },
}

impl ParseError {
    /// 构造 [`ParseError::InvalidInput`]。
    pub fn invalid(message: impl Into<String>) -> Self {
        ParseError::InvalidInput {
            message: message.into(),
        }
    }

    /// 字节不足时是否可通过继续喂入恢复。
    pub fn is_incomplete(&self) -> bool {
        matches!(self, ParseError::Incomplete { .. })
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Incomplete { needed, available } => match needed {
                Some(n) => write!(
                    f,
                    "incomplete input: need {n} more byte(s), {available} available"
                ),
                None => write!(f, "incomplete input: {available} byte(s) available"),
            },
            ParseError::LimitExceeded {
                requested,
                remaining,
            } => write!(
                f,
                "length limit exceeded: requested {requested} byte(s), {remaining} within bound"
            ),
            ParseError::InvalidInput { message } => write!(f, "invalid input: {message}"),
            ParseError::TrailingBytes { remaining } => {
                write!(f, "trailing bytes: {remaining} unconsumed")
            }
        }
    }
}

impl std::error::Error for ParseError {}

/// 解析结果别名。
pub type ParseResult<T> = Result<T, ParseError>;

/// 只读、带游标、带逻辑长度边界的字节解析器。
///
/// 生命周期参数 `'a` 与底层字节切片绑定；解析器零拷贝，
/// `read_bytes` / `take_subset` 返回的切片/子解析器直接借用原数据。
///
/// 索引约定：`pos` 是当前游标（绝对索引），`end` 是逻辑上允许读到
/// 的排他边界（绝对索引），`buf.len()` 是物理结尾。
/// 因此 `pos + n > end` 与 `pos + n > buf.len()` 可以区分出
/// “突破逻辑上限”和“物理字节不足”两类错误。
pub struct Reader<'a> {
    buf: &'a [u8],
    /// 本解析器起点在 `buf` 中的绝对索引（子集解析器非 0）
    origin: usize,
    /// 当前游标（绝对索引）
    pos: usize,
    /// 逻辑上允许读到的排他边界（绝对索引）
    end: usize,
}

impl<'a> Reader<'a> {
    /// 在完整切片上构造解析器，逻辑边界即物理结尾。
    pub fn new(buf: &'a [u8]) -> Self {
        Reader {
            buf,
            origin: 0,
            pos: 0,
            end: buf.len(),
        }
    }

    /// 构造一个**长度受限**的解析器：最多允许读取 `limit` 字节。
    ///
    /// - 当 `limit < buf.len()` 时，多读的部分即使物理存在也报
    ///   [`ParseError::LimitExceeded`]；
    /// - 物理缓冲本身不足时报 [`ParseError::Incomplete`]。
    pub fn limited(buf: &'a [u8], limit: usize) -> Self {
        Reader {
            buf,
            origin: 0,
            pos: 0,
            end: buf.len().min(limit),
        }
    }

    /// 已消费字节数（相对本解析器起点）。
    pub fn position(&self) -> usize {
        self.pos - self.origin
    }

    /// 逻辑边界内剩余可读字节数。
    pub fn remaining_len(&self) -> usize {
        self.end.saturating_sub(self.pos)
    }

    /// 物理上（含逻辑边界之外）剩余的字节数。
    pub fn physical_len(&self) -> usize {
        self.buf.len().saturating_sub(self.pos)
    }

    /// 逻辑边界内是否已读完。
    pub fn is_empty(&self) -> bool {
        self.pos >= self.end
    }

    /// 窥视当前字节但不移动游标；不足报 [`ParseError::Incomplete`]。
    pub fn peek_u8(&self) -> ParseResult<u8> {
        self.ensure(1)?;
        Ok(self.buf[self.pos])
    }

    /// 读取一个大端 u16。
    pub fn read_u16_be(&mut self) -> ParseResult<u16> {
        self.ensure(2)?;
        let v = u16::from_be_bytes([self.buf[self.pos], self.buf[self.pos + 1]]);
        self.pos += 2;
        Ok(v)
    }

    /// 读取一个大端 u32。
    pub fn read_u32_be(&mut self) -> ParseResult<u32> {
        self.ensure(4)?;
        let v = u32::from_be_bytes([
            self.buf[self.pos],
            self.buf[self.pos + 1],
            self.buf[self.pos + 2],
            self.buf[self.pos + 3],
        ]);
        self.pos += 4;
        Ok(v)
    }

    /// 读取单个字节。
    pub fn read_u8(&mut self) -> ParseResult<u8> {
        self.ensure(1)?;
        let v = self.buf[self.pos];
        self.pos += 1;
        Ok(v)
    }

    /// 连续读取 `n` 字节，返回零拷贝切片。
    pub fn read_bytes(&mut self, n: usize) -> ParseResult<&'a [u8]> {
        self.ensure(n)?;
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    /// 跳过 `n` 字节。
    pub fn skip(&mut self, n: usize) -> ParseResult<()> {
        self.ensure(n)?;
        self.pos += n;
        Ok(())
    }

    /// **子集**：切出接下来 `n` 字节对应的子解析器，同时父解析器
    /// 游标前进 `n`。子解析器读到第 `n+1` 字节即触发长度上限错误，
    /// 无论父缓冲物理上还有多少字节。
    pub fn take_subset(&mut self, n: usize) -> ParseResult<Reader<'a>> {
        self.ensure(n)?;
        let sub = Reader {
            buf: self.buf,
            origin: self.pos,
            pos: self.pos,
            end: (self.pos + n).min(self.end),
        };
        self.pos += n;
        Ok(sub)
    }

    /// 取出逻辑边界内尚未读取的全部字节（零拷贝），游标推进到边界。
    pub fn read_rest(&mut self) -> ParseResult<&'a [u8]> {
        let n = self.remaining_len();
        self.read_bytes(n)
    }

    /// 断言帧内字节已全部消费，否则报
    /// [`ParseError::TrailingBytes`]。
    pub fn finish(&self) -> ParseResult<()> {
        let remaining = self.remaining_len();
        if remaining == 0 {
            Ok(())
        } else {
            Err(ParseError::TrailingBytes { remaining })
        }
    }

    /// 借用底层完整切片（不受游标/边界影响）。
    pub fn as_inner(&self) -> &'a [u8] {
        self.buf
    }

    /// 读取前的统一边界校验：先判物理不足（Incomplete），
    /// 再判逻辑越界（LimitExceeded）。
    fn ensure(&self, n: usize) -> ParseResult<()> {
        let physical_available = self.buf.len().saturating_sub(self.pos);
        if n > physical_available {
            return Err(ParseError::Incomplete {
                needed: Some(n - physical_available),
                available: physical_available,
            });
        }
        if self.pos + n > self.end {
            return Err(ParseError::LimitExceeded {
                requested: n,
                remaining: self.end - self.pos,
            });
        }
        Ok(())
    }
}

impl<'a> fmt::Debug for Reader<'a> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Reader")
            .field("len", &self.buf.len())
            .field("pos", &self.pos)
            .field("end", &self.end)
            .field("remaining", &self.remaining_len())
            .finish()
    }
}

/// 增量字节缓冲：支持“随到随喂 + 不完整保留现场”。
///
/// 内部用一段连续 `Vec<u8>` 存放已喂入但未消费的字节；
/// 已消费前缀会在适当时机压缩，避免长连接下无限增长。
#[derive(Debug, Default)]
pub struct FeedBuffer {
    data: Vec<u8>,
    consumed: usize,
}

impl FeedBuffer {
    /// 新建空缓冲。
    pub fn new() -> Self {
        FeedBuffer {
            data: Vec::new(),
            consumed: 0,
        }
    }

    /// 带预分配容量新建。
    pub fn with_capacity(cap: usize) -> Self {
        FeedBuffer {
            data: Vec::with_capacity(cap),
            consumed: 0,
        }
    }

    /// 追加新到的字节（增量喂入）。
    pub fn feed(&mut self, bytes: &[u8]) {
        self.compact_if_needed();
        self.data.extend_from_slice(bytes);
    }

    /// 当前尚未消费的字节数。
    pub fn unconsumed_len(&self) -> usize {
        self.data.len() - self.consumed
    }

    /// 是否没有任何未消费字节。
    pub fn is_empty(&self) -> bool {
        self.unconsumed_len() == 0
    }

    /// 未消费字节的只读视图。
    pub fn unconsumed(&self) -> &[u8] {
        &self.data[self.consumed..]
    }

    /// 在未消费字节上构造一个普通 [`Reader`]（不消费任何字节）。
    pub fn reader(&self) -> Reader<'_> {
        Reader::new(self.unconsumed())
    }

    /// 在未消费字节上构造一个长度受限的 [`Reader`]。
    pub fn reader_limited(&self, limit: usize) -> Reader<'_> {
        Reader::limited(self.unconsumed(), limit)
    }

    /// 尝试用 `f` 解析当前缓冲。
    ///
    /// * 解析成功：消费实际读取的字节数，返回 `Ok(Some(value))`；
    /// * 数据不足（[`ParseError::Incomplete`]）：**不消费任何字节**，
    ///   返回 `Ok(None)`，等待后续 [`FeedBuffer::feed`]；
    /// * 其它错误（长度上限 / 非法字段 / 尾部冗余）：原样返回，
    ///   同样不消费字节，由调用方决定是否丢弃。
    pub fn try_parse<F, T>(&mut self, f: F) -> ParseResult<Option<T>>
    where
        F: FnOnce(&mut Reader<'_>) -> ParseResult<T>,
    {
        let mut reader = Reader::new(&self.data[self.consumed..]);
        match f(&mut reader) {
            Ok(value) => {
                let used = reader.position();
                self.consume(used);
                Ok(Some(value))
            }
            Err(ParseError::Incomplete { .. }) => Ok(None),
            Err(other) => Err(other),
        }
    }

    /// 消费未消费区前 `n` 个字节。
    pub fn consume(&mut self, n: usize) {
        let avail = self.unconsumed_len();
        assert!(n <= avail, "consume({n}) exceeds unconsumed {avail}");
        self.consumed += n;
        if self.consumed == self.data.len() {
            self.data.clear();
            self.consumed = 0;
        } else {
            self.compact_if_needed();
        }
    }

    /// 丢弃全部未消费字节（复位）。
    pub fn clear(&mut self) {
        self.data.clear();
        self.consumed = 0;
    }

    /// 当已消费前缀较大时前移，控制内存占用。
    fn compact_if_needed(&mut self) {
        if self.consumed == 0 {
            return;
        }
        if self.consumed >= 4096 || self.consumed * 2 >= self.data.len() {
            self.data.drain(..self.consumed);
            self.consumed = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_big_endian_values() {
        let mut r = Reader::new(&[1, 2, 3, 4, 5]);
        assert_eq!(r.position(), 0);
        assert_eq!(r.read_u8().unwrap(), 1);
        assert_eq!(r.read_u16_be().unwrap(), 0x0203);
        assert_eq!(
            r.read_u32_be().unwrap_err(),
            ParseError::Incomplete {
                needed: Some(2),
                available: 2,
            }
        );
        assert_eq!(r.read_bytes(2).unwrap(), &[4, 5]);
        assert!(r.is_empty());
    }

    #[test]
    fn limit_is_distinct_from_physical_end() {
        let buf = [1, 2, 3, 4];
        let mut r = Reader::limited(&buf, 2);
        assert_eq!(r.read_u16_be().unwrap(), 0x0102);
        // 物理上还有 2 字节，但逻辑上限是 2
        assert_eq!(
            r.read_u8().unwrap_err(),
            ParseError::LimitExceeded {
                requested: 1,
                remaining: 0,
            }
        );
    }

    #[test]
    fn subset_reader_cannot_escape_its_bounds() {
        let buf = [0u8, 1, 2, 3, 4, 5];
        let mut parent = Reader::new(&buf);
        assert_eq!(parent.read_u8().unwrap(), 0);
        let mut sub = parent.take_subset(2).unwrap();
        assert_eq!(sub.read_u8().unwrap(), 1);
        assert_eq!(sub.read_u8().unwrap(), 2);
        assert!(matches!(
            sub.read_u8().unwrap_err(),
            ParseError::LimitExceeded { .. }
        ));
        // 父解析器从子集之后继续
        assert_eq!(parent.read_u8().unwrap(), 3);
    }

    #[test]
    fn finish_detects_trailing_bytes() {
        let r = Reader::new(&[1, 2, 3]);
        assert!(matches!(
            r.finish().unwrap_err(),
            ParseError::TrailingBytes { remaining: 3 }
        ));
    }

    #[test]
    fn feed_buffer_reassembles_incrementally() {
        // 字节分两次到达：先 2 字节，读 u32 报“未到齐”且不消费
        let mut buf = FeedBuffer::new();
        buf.feed(&[0x00, 0x01]);
        assert!(buf.try_parse(|r| r.read_u32_be()).unwrap().is_none());
        assert_eq!(buf.unconsumed_len(), 2);

        // 再喂 2 字节，u32 读出，4 字节全部被消费
        buf.feed(&[0x00, 0x02]);
        let v = buf.try_parse(|r| r.read_u32_be()).unwrap().unwrap();
        assert_eq!(v, 0x0001_0002);
        assert_eq!(buf.unconsumed_len(), 0);
    }

    #[test]
    fn invalid_input_is_retained_not_consumed() {
        let mut buf = FeedBuffer::new();
        buf.feed(&[9, 9]);
        let err = buf
            .try_parse(|r| {
                let v = r.read_u8()?;
                if v != 1 {
                    return Err(ParseError::invalid("bad first byte"));
                }
                Ok(())
            })
            .unwrap_err();
        assert!(matches!(err, ParseError::InvalidInput { .. }));
        assert_eq!(buf.unconsumed_len(), 2); // 出错不消费
    }
}
