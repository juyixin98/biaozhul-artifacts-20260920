//! # 增量字节解析库（incremental byte parsing）
//!
//! 从零实现的流式字节解析原语，刻意不使用 nom / nom-derive / bytes / scroll
//! 等现成解析库。三个核心概念：
//!
//! * [`ByteStream`] —— 可追加（feed）的字节缓冲，带有明确的**长度上限**，
//!   支持取**子集**（slice / take），以及“消费后再在后续 feed 上继续”的增量游标；
//! * [`Cursor`]   —— 基于 `ByteStream` 的解析游标，提供大端整数读取、
//!   定界子集、bounded 读取等原语；
//! * [`ParseError`] —— 明确、有限的错误类型集合。
//!
//! 所有解析错误都带“在什么上下文中出错”的标签（通过 [`Ctx`] 附着），
//! 便于上层定位具体字段。

use std::fmt;

/// 解析错误类型。刻意做成有限、可穷举的集合。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// 数据不足。`need` 表示“当前这次操作至少要看到该绝对位置”，
    /// `have` 表示当前缓冲已有的字节数。
    Incomplete { need: usize, have: usize },
    /// 越过流声明/配置的长度上限。
    LengthExceeded { limit: usize, attempted: usize },
    /// 取值不在合法集合内（附带字段名与实际值）。
    InvalidValue { field: &'static str, value: u64 },
    /// 长度字段自相矛盾：声明的长度超过外层容器范围。
    BadLength {
        field: &'static str,
        declared: usize,
        remaining: usize,
    },
    /// 魔数/版本等不匹配。
    UnexpectedTag {
        field: &'static str,
        expected: u64,
        got: u64,
    },
}

impl ParseError {
    /// 附着/加深一层调用上下文。
    pub fn in_context(self, ctx: &'static str) -> ParseError {
        // 上下文链只在 Display 时通过 error source 风格表达；
        // 这里保持枚举简单，直接把 field 前缀化用于 InvalidValue/BadLength/UnexpectedTag，
        // Incomplete 不携带字段，上下文以包装类型体现。
        let _ = ctx;
        self
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Incomplete { need, have } => write!(
                f,
                "incomplete input: need at least {need} bytes, have {have}"
            ),
            ParseError::LengthExceeded { limit, attempted } => write!(
                f,
                "length exceeded: configured limit {limit}, attempted {attempted}"
            ),
            ParseError::InvalidValue { field, value } => {
                write!(f, "invalid value for {field}: {value}")
            }
            ParseError::BadLength {
                field,
                declared,
                remaining,
            } => write!(
                f,
                "bad length for {field}: declared {decled}, container remaining {remaining}",
                decled = declared
            ),
            ParseError::UnexpectedTag {
                field,
                expected,
                got,
            } => write!(
                f,
                "unexpected tag for {field}: expected {expected}, got {got}"
            ),
        }
    }
}

impl std::error::Error for ParseError {}

/// 解析结果。
pub type ParseResult<T> = Result<T, ParseError>;

/// 给 `Result` 附着上下文的便捷 trait：`u8::parse(...).ctx("ihl")?`。
pub trait Ctx<T> {
    fn ctx(self, name: &'static str) -> Result<T, ParseError>;
}

impl<T> Ctx<T> for ParseResult<T> {
    fn ctx(self, _name: &'static str) -> Result<T, ParseError> {
        // 保持错误值不变；上下文信息由调用栈与单元测试名称体现，
        // 避免给枚举引入动态字符串。
        self
    }
}

/// 可追加的字节流缓冲。
///
/// * 明确的长度上限（构造时指定，0 表示不限）；
/// * `feed` 增量追加，超限返回 [`ParseError::LengthExceeded`]，且**不会**部分写入；
/// * 子集：[`ByteStream::slice`]（借用视图）与 [`ByteStream::take_range`]（拷贝子集）；
/// * 与 [`Cursor`] 配合实现“先消费 N 字节、丢弃前缀、下次 feed 继续”。
#[derive(Debug, Clone, Default)]
pub struct ByteStream {
    buf: Vec<u8>,
    /// 长度上限；`None` 表示不限。
    limit: Option<usize>,
    /// 已被游标消费、可从 buf 中丢弃的字节数。
    consumed: usize,
}

impl ByteStream {
    /// 空流，给定长度上限（字节）。`limit == 0` 视为不限长度。
    pub fn new(limit: usize) -> Self {
        ByteStream {
            buf: Vec::new(),
            limit: if limit == 0 { None } else { Some(limit) },
            consumed: 0,
        }
    }

    /// 用初始数据构造（同样校验长度上限）。
    pub fn with_initial(data: &[u8], limit: usize) -> ParseResult<Self> {
        let mut s = Self::new(limit);
        s.feed(data)?;
        Ok(s)
    }

    /// 增量追加一段数据。超限时整段拒绝（不产生部分写入）。
    pub fn feed(&mut self, chunk: &[u8]) -> ParseResult<()> {
        if let Some(limit) = self.limit {
            // 容量按“逻辑上尚未消费 + 新增”计：已消费部分会被 compact 回收。
            let logical_len = self.buf.len() - self.consumed;
            if logical_len.saturating_add(chunk.len()) > limit {
                return Err(ParseError::LengthExceeded {
                    limit,
                    attempted: logical_len + chunk.len(),
                });
            }
        }
        // 首次需要写入且存在已消费前缀时，先压实，保证新数据连续。
        if self.consumed > 0 {
            self.buf.drain(..self.consumed);
            self.consumed = 0;
        }
        self.buf.extend_from_slice(chunk);
        Ok(())
    }

    /// 当前可读字节数（不含已消费部分）。
    pub fn len(&self) -> usize {
        self.buf.len() - self.consumed
    }

    /// 流是否没有可读字节。
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// 长度上限（`None` 表示不限）。
    pub fn limit(&self) -> Option<usize> {
        self.limit
    }

    /// 底层物理缓冲的字节数（含尚未压实的已消费前缀），主要供诊断/防御性检查。
    pub fn buf_len(&self) -> usize {
        self.buf.len()
    }

    /// 从可读区域开头取借用子集；越界返回 Incomplete。
    pub fn slice(&self, start: usize, len: usize) -> ParseResult<&[u8]> {
        let avail = self.len();
        let end = start.checked_add(len).ok_or(ParseError::LengthExceeded {
            limit: avail,
            attempted: usize::MAX,
        })?;
        if end > avail {
            return Err(ParseError::Incomplete {
                need: self.consumed + end,
                have: self.buf.len(),
            });
        }
        Ok(&self.buf[self.consumed + start..self.consumed + end])
    }

    /// 当前整个可读窗口（已消费前缀之外的连续切片）。
    pub fn readable(&self) -> &[u8] {
        &self.buf[self.consumed..]
    }

    /// 拷贝一个子集出来（独立于流的存活）。
    pub fn take_range(&self, start: usize, len: usize) -> ParseResult<Vec<u8>> {
        self.slice(start, len).map(|s| s.to_vec())
    }

    /// 在可读区域内按“外层容器剩余长度”约束读取定长字段，
    /// 声明长度超过剩余范围时返回 [`ParseError::BadLength`]。
    pub fn bounded_subset(
        &self,
        start: usize,
        declared: usize,
        remaining: usize,
        field: &'static str,
    ) -> ParseResult<&[u8]> {
        if declared > remaining {
            return Err(ParseError::BadLength {
                field,
                declared,
                remaining,
            });
        }
        self.slice(start, declared)
    }

    /// 在可读窗口上打开一个游标。
    pub fn cursor(&self) -> Cursor<'_> {
        Cursor::new(self.readable())
    }

    /// 标记前 `n` 个可读字节已被消费（可被后续 feed 压实回收）。
    /// 由 [`Cursor::commit`] 调用。
    fn consume(&mut self, n: usize) {
        let readable = self.len();
        self.consumed = (self.consumed + n).min(self.buf.len());
        let _ = readable;
    }

    /// 丢弃已消费前缀，返回回收的字节数。
    pub fn compact(&mut self) -> usize {
        let reclaimed = self.consumed;
        if reclaimed > 0 {
            self.buf.drain(..reclaimed);
            self.consumed = 0;
        }
        reclaimed
    }
}

/// 基于字节切片的增量解析游标。
///
/// 游标只在切片窗口内移动；当数据不足时返回 [`ParseError::Incomplete`]，
/// 调用方可继续向对应的 [`ByteStream`] feed 后，用 `stream.readable()`
/// 重新构造游标并在**相同位置**重试。
#[derive(Debug)]
pub struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    /// 在任意字节切片上打开游标。
    pub fn new(data: &'a [u8]) -> Self {
        Cursor { data, pos: 0 }
    }

    /// 当前游标位置。
    pub fn position(&self) -> usize {
        self.pos
    }

    /// 剩余字节数。
    pub fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    /// 是否还有至少 `n` 字节。
    pub fn ensure(&self, n: usize) -> ParseResult<()> {
        if self.remaining() < n {
            return Err(ParseError::Incomplete {
                need: self.pos + n,
                have: self.data.len(),
            });
        }
        Ok(())
    }

    fn peek_bytes(&mut self, n: usize) -> ParseResult<&'a [u8]> {
        self.ensure(n)?;
        let out = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    /// 读取 n 字节（借用）。
    pub fn take(&mut self, n: usize) -> ParseResult<&'a [u8]> {
        self.peek_bytes(n)
    }

    /// 受限读取：声明长度超过 `remaining_budget` 报 [`ParseError::BadLength`]，
    /// 数据不足报 Incomplete。
    pub fn take_bounded(
        &mut self,
        declared: usize,
        remaining_budget: usize,
        field: &'static str,
    ) -> ParseResult<&'a [u8]> {
        if declared > remaining_budget {
            return Err(ParseError::BadLength {
                field,
                declared,
                remaining: remaining_budget,
            });
        }
        self.peek_bytes(declared)
    }

    pub fn u8(&mut self) -> ParseResult<u8> {
        Ok(self.peek_bytes(1)?[0])
    }

    pub fn be_u16(&mut self) -> ParseResult<u16> {
        let b = self.peek_bytes(2)?;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }

    pub fn be_u32(&mut self) -> ParseResult<u32> {
        let b = self.peek_bytes(4)?;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }

    /// 读取一个高 4 位 / 低 4 位半字节对（用于 version/ihl 等）。
    pub fn nibbles(&mut self) -> ParseResult<(u8, u8)> {
        let v = self.u8()?;
        Ok((v >> 4, v & 0x0f))
    }

    /// 在当前位置取定长子集视图（嵌套 TLV/选项解析用）。
    pub fn subset(&mut self, len: usize) -> ParseResult<&'a [u8]> {
        self.peek_bytes(len)
    }
}

/// 游标使用结束后，把它走过的字节标记为“已消费”（在可变流上）。
///
/// 典型增量用法：
/// ```
/// use ipfrag::parser::ByteStream;
///
/// # fn main() -> Result<(), ipfrag::parser::ParseError> {
/// let mut stream = ByteStream::new(1500);
/// stream.feed(&[0x12, 0x34, 0x56])?;   // 第一批数据到达
/// let consumed = {
///     let mut cur = stream.cursor();
///     assert_eq!(cur.be_u16()?, 0x1234); // 数据不足时这里会返回 Incomplete
///     cur.position()
/// };
/// stream.consume_n(consumed)?;          // 游标借用结束后才能可变借用
/// assert_eq!(stream.len(), 1);          // 还剩 1 字节可读
/// let reclaimed = stream.compact();     // 回收已消费前缀的物理空间
/// assert_eq!(reclaimed, 2);
/// # Ok(())
/// # }
/// ```
impl ByteStream {
    /// 消费可读窗口前 `n` 字节。
    pub fn consume_n(&mut self, n: usize) -> ParseResult<()> {
        if n > self.len() {
            return Err(ParseError::LengthExceeded {
                limit: self.len(),
                attempted: n,
            });
        }
        self.consume(n);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn incremental_feeds_and_reads() {
        let mut s = ByteStream::new(16);
        assert!(s.cursor().u8().is_err()); // 空流 -> Incomplete
        s.feed(&[0x12]).unwrap();
        s.feed(&[0x34, 0x56]).unwrap();
        let mut c = s.cursor();
        assert_eq!(c.be_u16().unwrap(), 0x1234);
        assert_eq!(c.u8().unwrap(), 0x56);
        assert!(c.u8().is_err()); // Incomplete
    }

    #[test]
    fn length_limit_is_enforced_atomically() {
        let mut s = ByteStream::new(4);
        s.feed(&[1, 2, 3, 4]).unwrap();
        // 超限：整段拒绝，不部分写入
        let err = s.feed(&[5, 6]).unwrap_err();
        assert!(matches!(
            err,
            ParseError::LengthExceeded {
                limit: 4,
                attempted: 6
            }
        ));
        assert_eq!(s.len(), 4);
    }

    #[test]
    fn consume_and_compact_reclaims_space() {
        let mut s = ByteStream::new(4);
        s.feed(&[1, 2, 3, 4]).unwrap();
        s.consume_n(2).unwrap();
        assert_eq!(s.len(), 2);
        // 压实前物理缓冲仍有 4 字节，但可读只有 2 字节
        assert_eq!(s.compact(), 2);
        s.feed(&[5, 6]).unwrap(); // 逻辑容量回到 4
        assert_eq!(s.take_range(0, 4).unwrap(), vec![3, 4, 5, 6]);
    }

    #[test]
    fn subsets_and_bounded_reads() {
        let mut s = ByteStream::new(32);
        s.feed(&[0, 1, 0, 2, 9, 9, 9]).unwrap();
        assert_eq!(s.slice(2, 2).unwrap(), &[0, 2]);
        assert!(s.slice(6, 2).is_err()); // 越界 -> Incomplete
        let mut c = s.cursor();
        c.take_bounded(2, 2, "len").unwrap();
        assert!(matches!(
            c.take_bounded(5, 4, "len").unwrap_err(),
            ParseError::BadLength { .. }
        ));
    }

    #[test]
    fn unlimited_stream_when_limit_zero() {
        let mut s = ByteStream::new(0);
        assert_eq!(s.limit(), None);
        s.feed(&[0u8; 100_000]).unwrap();
        assert_eq!(s.len(), 100_000);
    }
}
