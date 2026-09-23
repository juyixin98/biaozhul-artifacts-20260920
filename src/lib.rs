//! mpstream — 增量（流式）解析 multipart/form-data 的一个**明确子集**。
//!
//! # 支持的子集（RFC 2046 §5.1 / RFC 7578 的裁剪版）
//!
//! * 消息 = 可选 preamble，随后 1..=N 个部件，最后以 `--boundary--` 结束。
//! * 每个部件 = 0 个或多个 `Name: value` 头（CRLF 分隔），空行，然后是原始字节正文。
//! * 部件分隔符为 `\r\n--boundary`，首个分隔符为 `--boundary`（可位于 preamble 之后）。
//! * 结束分隔符后允许一个可选的 `\r\n`，其后的 epilogue 一律忽略。
//! * 不支持：嵌套 multipart、Content-Transfer-Encoding 解码、按字符集转码、
//!   头折行（folding）。正文一律按不透明字节流处理。
//!
//! # 流式保证
//!
//! * 通过 [`MultipartParser::feed`] 以任意大小的块喂入字节；边界跨块被正确处理。
//! * 内部只保留一个分隔符长度的回看窗口（preamble 阶段同理），
//!   正文数据尽快以 [`Event::PartData`] 吐出，**不会缓存整个请求体**。
//! * 正文中出现的“近似边界”（如 `--boundary` 前面不是 CRLF、或后面跟的不是
//!   `\r\n` / `--`）一律视为正文数据，不会误判。

use std::fmt;

/// 解析限制。全部为硬上限，超限立即报错并拒绝继续解析。
#[derive(Debug, Clone)]
pub struct Limits {
    /// 最大部件数。
    pub max_parts: usize,
    /// 单个部件头部块（不含结尾空行）的最大字节数。
    pub max_header_bytes: usize,
    /// 整个输入流的最大字节数（含边界、头、正文）。
    pub max_total_bytes: usize,
    /// 单个部件正文的最大字节数。
    pub max_part_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_parts: 100,
            max_header_bytes: 8 * 1024,
            max_total_bytes: 16 * 1024 * 1024,
            max_part_bytes: 8 * 1024 * 1024,
        }
    }
}

/// 解析过程中产生的事件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// 一个部件的头已完整解析。`index` 从 0 开始。
    PartStart {
        index: usize,
        headers: Vec<(String, String)>,
    },
    /// 正文数据片段（流式，非空）。同一部件可能产生多次。
    PartData(Vec<u8>),
    /// 当前部件正文结束。
    PartEnd { index: usize },
    /// 已见到结束边界 `--boundary--`，整个消息解析完成。
    Done,
}

/// 解析错误类型。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// 边界字符串本身不合法（构造时检查）。
    InvalidBoundary(String),
    /// 输入总字节数超限。
    TotalSizeExceeded { limit: usize },
    /// 部件数量超限。
    TooManyParts { limit: usize },
    /// 头部块超限。
    HeaderTooLarge { limit: usize },
    /// 单部件正文超限。
    PartTooLarge { limit: usize },
    /// 头部格式错误（非 `Name: value`、非法字符等）。
    MalformedHeaders(String),
    /// 输入结束时未见到结束边界 `--boundary--`。
    MissingFinalBoundary,
    /// 解析已完成（Done）后仍继续喂数据。
    FeedAfterDone,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidBoundary(b) => write!(f, "invalid boundary: {b:?}"),
            Error::TotalSizeExceeded { limit } => write!(f, "total size exceeded limit {limit}"),
            Error::TooManyParts { limit } => write!(f, "too many parts (limit {limit})"),
            Error::HeaderTooLarge { limit } => write!(f, "header block exceeded limit {limit}"),
            Error::PartTooLarge { limit } => write!(f, "part body exceeded limit {limit}"),
            Error::MalformedHeaders(why) => write!(f, "malformed headers: {why}"),
            Error::MissingFinalBoundary => write!(f, "missing final boundary --<boundary>--"),
            Error::FeedAfterDone => write!(f, "feed called after Done"),
        }
    }
}

impl std::error::Error for Error {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    /// 尚未见到首个 `--boundary`。
    Preamble,
    /// 正在读取某个部件的头部块。
    Headers,
    /// 正在读取某个部件的正文。
    Body,
    /// 已见到 `--boundary--`，等待可选的尾部 CRLF。
    AfterFinal,
    /// 解析完成。
    Done,
}

/// 增量 multipart 解析器。用 [`MultipartParser::new`] 构造，
/// 反复调用 [`feed`](MultipartParser::feed)，输入结束时调用
/// [`finish`](MultipartParser::finish)。
pub struct MultipartParser {
    /// `--boundary`（首个分隔符）
    first_delim: Vec<u8>,
    /// `\r\n--boundary`（后续分隔符）
    delim: Vec<u8>,
    limits: Limits,
    buf: Vec<u8>,
    state: State,
    parts: usize,
    total: usize,
    part_bytes: usize,
}

impl MultipartParser {
    /// 构造解析器。`boundary` 为 Content-Type 中 boundary= 的值（不含前导 `--`）。
    pub fn new(boundary: &str, limits: Limits) -> Result<Self, Error> {
        validate_boundary(boundary)?;
        Ok(Self::with_delims(boundary, limits))
    }

    fn with_delims(boundary: &str, limits: Limits) -> Self {
        let mut first_delim = Vec::with_capacity(boundary.len() + 2);
        first_delim.extend_from_slice(b"--");
        first_delim.extend_from_slice(boundary.as_bytes());
        let mut delim = Vec::with_capacity(boundary.len() + 4);
        delim.extend_from_slice(b"\r\n--");
        delim.extend_from_slice(boundary.as_bytes());
        MultipartParser {
            first_delim,
            delim,
            limits,
            buf: Vec::new(),
            state: State::Preamble,
            parts: 0,
            total: 0,
            part_bytes: 0,
        }
    }

    /// 喂入一块字节，返回本次产生的事件（可能为空、可能多个）。
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Event>, Error> {
        if self.state == State::Done {
            return Err(Error::FeedAfterDone);
        }
        self.total += chunk.len();
        if self.total > self.limits.max_total_bytes {
            return Err(Error::TotalSizeExceeded {
                limit: self.limits.max_total_bytes,
            });
        }
        self.buf.extend_from_slice(chunk);
        self.process()
    }

    /// 输入流结束时调用。若未见到结束边界则报错；否则返回剩余事件（通常为 `Done`）。
    pub fn finish(&mut self) -> Result<Vec<Event>, Error> {
        match self.state {
            State::Done => Ok(Vec::new()),
            State::AfterFinal => {
                self.state = State::Done;
                self.buf.clear();
                Ok(vec![Event::Done])
            }
            _ => Err(Error::MissingFinalBoundary),
        }
    }

    /// 已接收的部件数。
    pub fn parts_seen(&self) -> usize {
        self.parts
    }

    /// 已接收的总字节数。
    pub fn total_seen(&self) -> usize {
        self.total
    }

    fn process(&mut self) -> Result<Vec<Event>, Error> {
        let mut events = Vec::new();
        loop {
            match self.state {
                State::Preamble => {
                    match find_subslice(&self.buf, &self.first_delim) {
                        Some(i) => {
                            let after = i + self.first_delim.len();
                            if self.buf.len() < after + 2 {
                                // 等更多字节来判断 -- 还是 CRLF；先不消费任何字节，
                                // 保证状态机可重入。
                                return Ok(events);
                            }
                            let tail = &self.buf[after..after + 2];
                            let is_final = tail == b"--";
                            let is_next = tail == b"\r\n";
                            if is_final || is_next {
                                self.buf.drain(..after + 2);
                                self.state = if is_final {
                                    State::AfterFinal
                                } else {
                                    State::Headers
                                };
                            } else {
                                // preamble 里的假边界（--boundary 后接其他字节），
                                // 丢弃一个字节继续扫描。
                                self.buf.drain(..i + 1);
                            }
                        }
                        None => {
                            // 保留一个分隔符长度的回看窗口，其余作为 preamble 丢弃。
                            let keep = self.first_delim.len();
                            if self.buf.len() > keep {
                                let drop = self.buf.len() - keep;
                                self.buf.drain(..drop);
                            }
                            return Ok(events);
                        }
                    }
                }
                State::Headers => {
                    // 零头部的部件：边界行后直接是空行。
                    if self.buf.starts_with(b"\r\n") {
                        self.buf.drain(..2);
                        self.begin_part(&mut events)?;
                        continue;
                    }
                    match find_subslice(&self.buf, b"\r\n\r\n") {
                        Some(i) => {
                            if i > self.limits.max_header_bytes {
                                return Err(Error::HeaderTooLarge {
                                    limit: self.limits.max_header_bytes,
                                });
                            }
                            let header_block: Vec<u8> = self.buf.drain(..i + 4).take(i).collect();
                            let headers = parse_headers(&header_block)?;
                            self.begin_part_with(&mut events, headers)?;
                        }
                        None => {
                            if self.buf.len() > self.limits.max_header_bytes {
                                return Err(Error::HeaderTooLarge {
                                    limit: self.limits.max_header_bytes,
                                });
                            }
                            return Ok(events);
                        }
                    }
                }
                State::Body => {
                    match find_subslice(&self.buf, &self.delim) {
                        Some(i) => {
                            let after = i + self.delim.len();
                            if self.buf.len() < after + 2 {
                                // 分隔符出现在缓冲末尾，无法判断是部件边界还是
                                // 正文里的“近似边界”，等更多字节。
                                return Ok(events);
                            }
                            let tail = &self.buf[after..after + 2];
                            let is_final = tail == b"--";
                            let is_next = tail == b"\r\n";
                            if is_final || is_next {
                                // 真正的边界。
                                let data: Vec<u8> = self.buf.drain(..i).collect();
                                self.buf.drain(..self.delim.len() + 2);
                                self.push_part_data(&mut events, data)?;
                                events.push(Event::PartEnd {
                                    index: self.parts - 1,
                                });
                                self.state = if is_final {
                                    State::AfterFinal
                                } else {
                                    State::Headers
                                };
                            } else {
                                // 近似边界：`\r\n--boundary` 后面跟了别的字节，
                                // 这整段都是正文数据，冲刷后继续扫描。
                                let data: Vec<u8> = self.buf.drain(..after).collect();
                                self.push_part_data(&mut events, data)?;
                            }
                        }
                        None => {
                            // 保留 delim.len() 字节回看窗口，其余作为正文冲刷。
                            if self.buf.len() > self.delim.len() {
                                let n = self.buf.len() - self.delim.len();
                                let data: Vec<u8> = self.buf.drain(..n).collect();
                                self.push_part_data(&mut events, data)?;
                            }
                            return Ok(events);
                        }
                    }
                }
                State::AfterFinal => {
                    if self.buf.len() < 2 {
                        // 等可选的 CRLF；若对端直接关闭，finish() 会收尾。
                        return Ok(events);
                    }
                    if self.buf.starts_with(b"\r\n") {
                        self.buf.drain(..2);
                    }
                    // epilogue 一律忽略。
                    self.buf.clear();
                    self.state = State::Done;
                    events.push(Event::Done);
                    return Ok(events);
                }
                State::Done => return Ok(events),
            }
        }
    }

    fn begin_part(&mut self, events: &mut Vec<Event>) -> Result<(), Error> {
        self.begin_part_with(events, Vec::new())
    }

    fn begin_part_with(
        &mut self,
        events: &mut Vec<Event>,
        headers: Vec<(String, String)>,
    ) -> Result<(), Error> {
        self.parts += 1;
        if self.parts > self.limits.max_parts {
            return Err(Error::TooManyParts {
                limit: self.limits.max_parts,
            });
        }
        self.part_bytes = 0;
        self.state = State::Body;
        events.push(Event::PartStart {
            index: self.parts - 1,
            headers,
        });
        Ok(())
    }

    fn push_part_data(&mut self, events: &mut Vec<Event>, data: Vec<u8>) -> Result<(), Error> {
        if data.is_empty() {
            return Ok(());
        }
        self.part_bytes += data.len();
        if self.part_bytes > self.limits.max_part_bytes {
            return Err(Error::PartTooLarge {
                limit: self.limits.max_part_bytes,
            });
        }
        events.push(Event::PartData(data));
        Ok(())
    }
}

/// 校验 boundary 是否合法（RFC 2046：1..=70 字符，限定字符集，结尾不能是空格）。
fn validate_boundary(b: &str) -> Result<(), Error> {
    fn ok_char(c: u8) -> bool {
        c.is_ascii_alphanumeric()
            || matches!(
                c,
                b'\'' | b'(' | b')' | b'+' | b'_' | b',' | b'-' | b'.' | b'/' | b':' | b'=' | b'?'
                    | b' '
            )
    }
    if b.is_empty() || b.len() > 70 {
        return Err(Error::InvalidBoundary(b.to_string()));
    }
    if !b.bytes().all(ok_char) || b.ends_with(' ') {
        return Err(Error::InvalidBoundary(b.to_string()));
    }
    Ok(())
}

/// 朴素子串查找。分隔符很短（<= 74 字节），且只在缓冲窗口内查找，足够高效。
fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack
        .windows(needle.len())
        .position(|w| w == needle)
}

/// 解析头部块（不含结尾空行）。允许空头部块。
fn parse_headers(block: &[u8]) -> Result<Vec<(String, String)>, Error> {
    let mut headers = Vec::new();
    if block.is_empty() {
        return Ok(headers);
    }
    for line in block.split(|&b| b == b'\n') {
        // 去掉行尾 \r（split 后每行以 \r 结尾，最后一行除外——但整块以 \r\n\r\n
        // 之前的 \r\n 切出，因此每行都有 \r 结尾，最后一行也可能没有）。
        let line = match line.last() {
            Some(b'\r') => &line[..line.len() - 1],
            _ => line,
        };
        if line.is_empty() {
            continue;
        }
        let colon = line
            .iter()
            .position(|&b| b == b':')
            .ok_or_else(|| Error::MalformedHeaders(format!("no colon in line {line:?}")))?;
        let name = &line[..colon];
        if name.is_empty() || !name.iter().all(|&c| is_token_char(c)) {
            return Err(Error::MalformedHeaders(format!(
                "bad header name in line {line:?}"
            )));
        }
        let mut value = &line[colon + 1..];
        if value.first() == Some(&b' ') {
            value = &value[1..];
        }
        let name = std::str::from_utf8(name)
            .map_err(|_| Error::MalformedHeaders("header name not UTF-8".into()))?
            .to_string();
        let value = String::from_utf8_lossy(value).into_owned();
        headers.push((name, value));
    }
    Ok(headers)
}

/// RFC 7230 token 字符（头部字段名）。
fn is_token_char(c: u8) -> bool {
    c.is_ascii_alphanumeric()
        || matches!(
            c,
            b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.' | b'^' | b'_'
                | b'`' | b'|' | b'~'
        )
}
