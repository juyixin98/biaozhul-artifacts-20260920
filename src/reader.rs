//! 手写的增量 multipart/form-data 解析器（本 crate 的核心）。
//!
//! ## 设计
//!
//! 解析器是一个字节驱动的状态机（[`MultipartReader`]）。调用方把**任意大小、任意切分**
//! 的 TCP 字节块依次喂入 [`MultipartReader::feed`]，得到 0..N 个 [`Event`]。
//!
//! 关键性质：
//!
//! - **边界跨块**：起始边界、分隔边界、头块结尾 `\r\n\r\n` 都允许横跨任意多个 `feed`。
//!   做法是每次只“确认”不可能再参与匹配的字节为正文，把“可能是分隔符前缀”的尾巴留在内部缓冲区。
//! - **二进制安全 / 近似边界不误判**：正文状态下匹配的是完整的 `CRLF "--" boundary`，
//!   且其后必须跟 `CRLF` 或 `--`。缺前导 CRLF（例如正文里裸出现 `--boundary`）、
//!   后缀为单个 `-`、`-x` 等都判定为正文并从疑似位置之后继续扫描。
//! - **不缓存完整正文**：正文状态下内部缓冲区只保留
//!   `分隔符长度 + 1` 量级的“待分类尾巴”（boundary 70 时也只有 75 字节），
//!   另加至多 `max_headers_size` 的未结束头块；两者都与已传输正文总量无关
//!   （[`MultipartReader::buffered_len`] 可观测，测试据此断言）。
//! - **超限即错**：总字节、单 part 正文、头块、part 数量四类限制分别对应明确错误。

use std::collections::BTreeMap;

use crate::error::Error;
use crate::event::{Event, PartMeta};
use crate::limits::Limits;
use crate::mime;

/// feed 处理器返回“需要更多数据、保留整个缓冲区”时使用的哨兵。
const NEED_MORE: usize = usize::MAX;

/// 解析器内部状态。
#[derive(Debug)]
enum State {
    /// 等待起始边界 `--boundary`（本实现不支持 preamble）。
    Start,
    /// 起始边界之后分类两个收尾字节：`\r`→(等\n，进入头) 或 `-`→(等-，结束)。
    FirstTerm { matched: u8 },
    /// 累积当前 part 的头块，直到 `\r\n\r\n`。
    Headers,
    /// 正文扫描中，寻找 `\r\n--boundary`，再分类后续两个字节。
    Body,
    /// 已遇关闭边界 `--boundary--`，只允许可选的 CRLF。
    AfterClose { saw_cr: bool },
    /// 流完全结束（关闭边界 + CRLF 已消费）。
    Done,
}

/// 增量 multipart 解析器。
#[derive(Debug)]
pub struct MultipartReader {
    boundary: Vec<u8>,
    limits: Limits,
    state: State,
    /// 未决字节：可能是待分类正文尾巴、半截头块或半截分隔符。
    buf: Vec<u8>,
    /// 已计入的整流字节数（含所有喂入字节，饱和加法）。
    total: u64,
    /// 已开始的 part 数（头块完整解析时计数）。
    parts_seen: usize,
    /// 当前 part 已确认正文的字节数（精确，不含留在未决区里的疑似前缀）。
    part_bytes: u64,
}

impl MultipartReader {
    /// 以 boundary 原始值（不含前导 `--`）与限制创建解析器。boundary 非法时返回错误。
    pub fn new(boundary: &[u8], limits: Limits) -> Result<Self, Error> {
        mime::validate_boundary(boundary)?;
        Ok(MultipartReader {
            boundary: boundary.to_vec(),
            limits,
            state: State::Start,
            buf: Vec::new(),
            total: 0,
            parts_seen: 0,
            part_bytes: 0,
        })
    }

    /// 喂入一段字节（可以是任意长度，包括 0），返回这段输入解析出的事件。
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Event>, Error> {
        self.total = self.total.saturating_add(chunk.len() as u64);
        if self.total > self.limits.max_total_size {
            return Err(Error::TotalTooLarge);
        }
        self.buf.extend_from_slice(chunk);

        let mut events = Vec::new();
        loop {
            let cut = match self.state {
                State::Start => self.run_start()?,
                State::FirstTerm { matched } => self.run_first_term(matched, &mut events)?,
                State::Headers => self.run_headers(&mut events)?,
                State::Body => self.run_body(&mut events)?,
                State::AfterClose { saw_cr } => self.run_after_close(saw_cr)?,
                State::Done => {
                    if self.buf.is_empty() {
                        break;
                    } else {
                        return Err(Error::MalformedStream);
                    }
                }
            };
            if cut == NEED_MORE {
                break;
            }
            if cut > 0 {
                self.buf.drain(..cut);
            }
        }
        Ok(events)
    }

    /// 声明上游不会再有字节。正常时返回剩余事件（通常为空，`End` 在关闭边界确认时已发出）；
    /// 若解析器还在等待任何必需字节，返回 [`Error::Truncated`]。
    pub fn finish(self) -> Result<Vec<Event>, Error> {
        match self.state {
            State::Done => {
                if self.buf.is_empty() {
                    Ok(Vec::new())
                } else {
                    Err(Error::MalformedStream)
                }
            }
            State::AfterClose { saw_cr: false } if self.buf.is_empty() => Ok(Vec::new()),
            // 关闭边界后挂着一个裸 CR：CRLF 不完整，输入已结束 → 截断。
            State::AfterClose { .. } => Err(Error::Truncated),
            _ => Err(Error::Truncated),
        }
    }

    /// 当前内部缓冲区保留的字节数（流式测试用：证明不做整段缓存）。
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }

    /// 当前是否处于正文状态（测试用）。
    pub fn in_body(&self) -> bool {
        matches!(self.state, State::Body)
    }

    // ------------------------------------------------------------ 各状态处理器

    /// 起始边界：`--boundary`，必须位于流的最开头（逐块前缀匹配）。
    fn run_start(&mut self) -> Result<usize, Error> {
        let d = self.open_delim();
        let n = self.buf.len().min(d.len());
        if self.buf[..n] != d[..n] {
            return Err(Error::MissingStartBoundary);
        }
        if n < d.len() {
            Ok(NEED_MORE) // 已到的字节全是起始边界前缀，全部保留
        } else {
            self.state = State::FirstTerm { matched: 0 };
            Ok(d.len())
        }
    }

    /// 起始边界后的两个分类字节。
    fn run_first_term(&mut self, matched: u8, events: &mut Vec<Event>) -> Result<usize, Error> {
        if self.buf.is_empty() {
            return Ok(NEED_MORE);
        }
        let b = self.buf[0];
        match (matched, b) {
            (0, b'\r') => {
                self.state = State::FirstTerm { matched: 1 };
                Ok(1)
            }
            (1, b'\n') => {
                self.state = State::Headers;
                Ok(1)
            }
            (0, b'-') => {
                self.state = State::FirstTerm { matched: 2 };
                Ok(1)
            }
            (2, b'-') => {
                self.state = State::AfterClose { saw_cr: false };
                events.push(Event::End);
                Ok(1)
            }
            _ => Err(Error::MalformedStream),
        }
    }

    /// 头块累积与解析。
    fn run_headers(&mut self, events: &mut Vec<Event>) -> Result<usize, Error> {
        // 空头部块（分隔行后直接又是 CRLF）：明确拒绝。
        if self.buf.starts_with(b"\r\n") {
            return Err(Error::MissingDisposition);
        }
        let end = match find_subseq(&self.buf, b"\r\n\r\n") {
            Some(i) => i,
            None => {
                if self.buf.len() > self.limits.max_headers_size {
                    return Err(Error::HeaderTooLarge);
                }
                return Ok(NEED_MORE);
            }
        };
        if end + 4 > self.limits.max_headers_size {
            return Err(Error::HeaderTooLarge);
        }
        self.parts_seen += 1;
        if self.parts_seen > self.limits.max_parts {
            return Err(Error::TooManyParts);
        }
        let (name, filename, extras) = mime::parse_part_headers(&self.buf[..end])?;
        let mut headers = BTreeMap::new();
        for (k, v) in extras {
            headers.entry(k).or_insert(v);
        }
        self.part_bytes = 0;
        events.push(Event::PartBegin(PartMeta {
            name,
            filename,
            headers,
        }));
        self.state = State::Body;
        Ok(end + 4)
    }

    /// 正文扫描（最核心的状态，详见模块注释）。
    fn run_body(&mut self, events: &mut Vec<Event>) -> Result<usize, Error> {
        let d = self.full_delim(); // \r\n--boundary
        let mut scan = 0usize;

        loop {
            // 情况 A：剩余数据比完整分隔符还短，只可能存在“前缀尾巴”。
            if self.buf.len() - scan < d.len() {
                let keep = suffix_prefix_len(&self.buf[scan..], &d);
                let split = self.buf.len() - keep;
                self.emit_body(scan..split, events)?;
                return Ok(if split == 0 { NEED_MORE } else { split });
            }

            // 情况 B：在未确认区间搜索完整分隔符。
            let m = match find_subseq(&self.buf[scan..], &d) {
                Some(rel) => scan + rel,
                // 没找到完整分隔符：保留“是分隔符前缀”的最长后缀，其余确认为正文。
                None => {
                    let keep = suffix_prefix_len(&self.buf[scan..], &d);
                    let split = self.buf.len() - keep;
                    self.emit_body(scan..split, events)?;
                    return Ok(if split == 0 { NEED_MORE } else { split });
                }
            };

            // 完整分隔符之后还需要两个分类字节（\r\n 或 --），不够就整体保留等数据。
            if m + d.len() + 1 >= self.buf.len() {
                self.emit_body(scan..m, events)?;
                // m==0 时整个缓冲区都是待分类候选，必须“需要更多数据”，否则外层会空转。
                return Ok(if m == 0 { NEED_MORE } else { m });
            }
            let term = self.buf[m + d.len()];
            let after = self.buf[m + d.len() + 1];
            match (term, after) {
                (b'\r', b'\n') => {
                    // 真·分隔行：本 part 结束，后续是下一个 part 的头。
                    self.emit_body(scan..m, events)?;
                    events.push(Event::PartEnd);
                    self.state = State::Headers;
                    return Ok(m + d.len() + 2);
                }
                (b'-', b'-') => {
                    // 真·关闭边界。
                    self.emit_body(scan..m, events)?;
                    events.push(Event::PartEnd);
                    events.push(Event::End);
                    self.state = State::AfterClose { saw_cr: false };
                    return Ok(m + d.len() + 2);
                }
                // 近似边界：分隔符前缀成立但后缀不对 → 全部是正文。
                // [scan..m] 之间不可能再有 d（find_subseq 未发现）；候选首字节
                // buf[m]（即 '\r'）也不可能属于另一条真分隔符（d 固定以 \r\n 开头，
                // 以 m 为起点的匹配已被证伪，m+1 是 '\n'、m+2 是 '-' 都不能另起匹配），
                // 因此立即确认到 m+1，从 m+1 继续扫描，避免这些字节在后续提前返回时丢失。
                _ => {
                    self.emit_body(scan..m + 1, events)?;
                    scan = m + 1;
                }
            }
        }
    }

    /// 关闭边界之后：允许 CRLF，然后必须是流尾；不支持 epilogue / transport padding。
    fn run_after_close(&mut self, saw_cr: bool) -> Result<usize, Error> {
        if self.buf.is_empty() {
            return Ok(NEED_MORE);
        }
        if saw_cr {
            if self.buf[0] == b'\n' {
                self.state = State::Done;
                Ok(1)
            } else {
                Err(Error::MalformedStream)
            }
        } else if self.buf[0] == b'\r' {
            self.state = State::AfterClose { saw_cr: true };
            Ok(1)
        } else {
            Err(Error::MalformedStream)
        }
    }

    // ------------------------------------------------------------ 辅助

    fn open_delim(&self) -> Vec<u8> {
        let mut v = Vec::with_capacity(self.boundary.len() + 2);
        v.extend_from_slice(b"--");
        v.extend_from_slice(&self.boundary);
        v
    }

    fn full_delim(&self) -> Vec<u8> {
        let mut v = Vec::with_capacity(self.boundary.len() + 4);
        v.extend_from_slice(b"\r\n--");
        v.extend_from_slice(&self.boundary);
        v
    }

    /// 确认一段字节属于当前 part 正文：计数、查单 part 上限、发 Body 事件。
    fn emit_body(
        &mut self,
        range: std::ops::Range<usize>,
        events: &mut Vec<Event>,
    ) -> Result<(), Error> {
        if range.end > range.start {
            self.part_bytes = self
                .part_bytes
                .saturating_add((range.end - range.start) as u64);
            if self.part_bytes > self.limits.max_part_size {
                return Err(Error::PartTooLarge);
            }
            events.push(Event::Body(self.buf[range].to_vec()));
        }
        Ok(())
    }
}

/// 在 `hay` 中查找 `needle` 第一次出现的位置（朴素窗口匹配，needle ≤ 74 字节）。
fn find_subseq(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    (0..=hay.len() - needle.len()).find(|&i| &hay[i..i + needle.len()] == needle)
}

/// 返回最大的 k（0..needle.len()），使 `hay` 以 `needle[..k]` 结尾。
/// 即“当前未决区末尾有多长一段可能是跨块分隔符的前缀”。
fn suffix_prefix_len(hay: &[u8], needle: &[u8]) -> usize {
    let max_k = hay.len().min(needle.len() - 1);
    for k in (0..=max_k).rev() {
        if hay[hay.len() - k..] == needle[..k] {
            return k;
        }
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helper_math() {
        assert_eq!(find_subseq(b"xxabcxx", b"abc"), Some(2));
        assert_eq!(find_subseq(b"abc", b"abcd"), None);
        assert_eq!(suffix_prefix_len(b"abc", b"abcd"), 3);
        assert_eq!(suffix_prefix_len(b"abx", b"abcd"), 0);
        assert_eq!(suffix_prefix_len(b"zab", b"abcd"), 2);
        assert_eq!(suffix_prefix_len(b"", b"abcd"), 0);
    }
}
