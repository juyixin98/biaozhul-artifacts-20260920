//! 手写 SSE 增量字节解析器（客户端解码侧）。
//!
//! ## 支持的协议子集
//! - 字段：`data`（可多行，派发时以 `\n` 连接）、`event`、`id`、`retry`。
//! - 注释行：以 `:` 开头的行被忽略（可用作心跳）。
//! - 未知字段：忽略。
//! - 行尾：`\n`、`\r`、`\r\n` 均支持，且 `\r\n` 可跨块拆分。
//! - 无冒号的行：整行作为字段名，值为空串。
//! - `id` 含 NUL 字符时按规范忽略该字段。
//! - `retry` 非纯数字时按规范忽略。
//!
//! ## 长度上限（可通过 [`Limits`] 调整）
//! - 单行 8 KiB、单事件 data 累计 64 KiB、字段名 64 字节、id 256 字节。
//!
//! ## 错误
//! 超过上限或出现非法 UTF-8 时返回 [`ParseError`]；出错后解析器状态未定义，
//! 调用方应丢弃并重建（对应断线重连语义）。

use std::fmt;

/// 解析器资源上限。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 单行最大字节数（不含行尾）。
    pub max_line_bytes: usize,
    /// 单个事件 data 缓冲最大字节数。
    pub max_event_bytes: usize,
    /// 字段名最大字节数。
    pub max_field_bytes: usize,
    /// id 值最大字节数。
    pub max_id_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_line_bytes: 8 * 1024,
            max_event_bytes: 64 * 1024,
            max_field_bytes: 64,
            max_id_bytes: 256,
        }
    }
}

/// 解析错误类型。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// 单行超过长度上限。
    LineTooLong { limit: usize },
    /// 单事件 data 累计超过上限。
    EventTooLarge { limit: usize },
    /// 字段名超过上限。
    FieldNameTooLong { limit: usize },
    /// id 超过上限。
    IdTooLong { limit: usize },
    /// data / event / id 字段值不是合法 UTF-8。
    InvalidUtf8,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::LineTooLong { limit } => write!(f, "line exceeds {limit} bytes"),
            ParseError::EventTooLarge { limit } => {
                write!(f, "event data exceeds {limit} bytes")
            }
            ParseError::FieldNameTooLong { limit } => {
                write!(f, "field name exceeds {limit} bytes")
            }
            ParseError::IdTooLong { limit } => write!(f, "id exceeds {limit} bytes"),
            ParseError::InvalidUtf8 => write!(f, "invalid UTF-8 in field value"),
        }
    }
}

impl std::error::Error for ParseError {}

/// 一个派发出来的完整事件。`event` 缺省为 `"message"`；`id` 为派发时刻的
/// last-event-id（可能为空串，表示从未设置或被空 id 重置）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub id: String,
    pub event: String,
    pub data: String,
}

/// 解析器输出：要么是事件，要么是服务端要求的重连间隔（毫秒）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Output {
    Event(Event),
    Retry(u64),
}

/// 增量 SSE 解析器。通过 [`Parser::feed`] 逐块喂入字节，
/// 每次返回本次喂入期间完整派生出的输出。
pub struct Parser {
    limits: Limits,
    /// 当前未完成的行。
    line: Vec<u8>,
    /// 上一块以 `\r` 结尾：若下个字节是 `\n` 则吞掉（CRLF 跨块）。
    skip_lf: bool,
    /// data 累积缓冲（每行追加 value + '\n'）。
    data: Vec<u8>,
    /// 当前事件的 event 类型缓冲。
    event_type: Option<String>,
    /// last-event-id 缓冲（规范语义：id 字段即时更新，空行派发时落入 last_event_id）。
    id_buf: String,
    /// 最近一次派发时的 last-event-id。
    last_event_id: String,
}

impl Default for Parser {
    fn default() -> Self {
        Self::new()
    }
}

impl Parser {
    pub fn new() -> Self {
        Self::with_limits(Limits::default())
    }

    pub fn with_limits(limits: Limits) -> Self {
        Parser {
            limits,
            line: Vec::new(),
            skip_lf: false,
            data: Vec::new(),
            event_type: None,
            id_buf: String::new(),
            last_event_id: String::new(),
        }
    }

    /// 当前 last-event-id（重连时放入 Last-Event-ID 头）。
    pub fn last_event_id(&self) -> &str {
        &self.last_event_id
    }

    /// 喂入一块字节，返回期间产生的完整输出。
    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Output>, ParseError> {
        let mut out = Vec::new();
        for &b in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if b == b'\n' {
                    continue; // CRLF 的 LF 部分（可能跨块）
                }
            }
            match b {
                b'\r' => {
                    self.end_line(&mut out)?;
                    self.skip_lf = true;
                }
                b'\n' => {
                    self.end_line(&mut out)?;
                }
                _ => {
                    if self.line.len() >= self.limits.max_line_bytes {
                        return Err(ParseError::LineTooLong {
                            limit: self.limits.max_line_bytes,
                        });
                    }
                    self.line.push(b);
                }
            }
        }
        Ok(out)
    }

    /// 连接结束时调用：若缓冲区还残留未以行尾结束的一行，按规范丢弃，
    /// 调用方应重连续传。返回当前 last-event-id 便于直接重连。
    pub fn finish(&mut self) -> String {
        self.line.clear();
        self.skip_lf = false;
        // 未派发完的 data/event_type 一并丢弃，等待重放。
        self.data.clear();
        self.event_type = None;
        self.last_event_id.clone()
    }

    fn end_line(&mut self, out: &mut Vec<Output>) -> Result<(), ParseError> {
        let line = std::mem::take(&mut self.line);
        if line.is_empty() {
            // 空行：派发事件。
            self.dispatch(out);
            return Ok(());
        }
        if line[0] == b':' {
            // 注释行（心跳），忽略。
            return Ok(());
        }
        // 拆分字段名与值；无冒号则整行为字段名、值为空。
        let (name, value) = match line.iter().position(|&c| c == b':') {
            Some(i) => {
                let mut v = &line[i + 1..];
                if v.first() == Some(&b' ') {
                    v = &v[1..]; // 去掉值前恰好一个空格
                }
                (&line[..i], v)
            }
            None => (&line[..], &line[..0]),
        };
        if name.len() > self.limits.max_field_bytes {
            return Err(ParseError::FieldNameTooLong {
                limit: self.limits.max_field_bytes,
            });
        }
        match name {
            b"data" => {
                if self.data.len() + value.len() + 1 > self.limits.max_event_bytes {
                    return Err(ParseError::EventTooLarge {
                        limit: self.limits.max_event_bytes,
                    });
                }
                // 逐行校验 UTF-8：行分隔符为 ASCII，不会切断多字节序列，
                // 因此逐行校验等价于整体校验，且让 dispatch 无失败路径。
                std::str::from_utf8(value).map_err(|_| ParseError::InvalidUtf8)?;
                self.data.extend_from_slice(value);
                self.data.push(b'\n');
            }
            b"event" => {
                self.event_type = Some(String::from_utf8(value.to_vec()).map_err(|_| ParseError::InvalidUtf8)?);
            }
            b"id" => {
                if value.contains(&0) {
                    // 规范：含 NUL 的 id 字段忽略。
                    return Ok(());
                }
                if value.len() > self.limits.max_id_bytes {
                    return Err(ParseError::IdTooLong {
                        limit: self.limits.max_id_bytes,
                    });
                }
                self.id_buf =
                    String::from_utf8(value.to_vec()).map_err(|_| ParseError::InvalidUtf8)?;
            }
            // 纯数字才生效，否则忽略。全为 ASCII 数字，直接按字节解析。
            b"retry" if !value.is_empty() && value.iter().all(|c| c.is_ascii_digit()) => {
                let ms = value.iter().fold(0u64, |acc, c| {
                    acc.saturating_mul(10).saturating_add((c - b'0') as u64)
                });
                out.push(Output::Retry(ms));
            }
            b"retry" => {}
            _ => {
                // 未知字段：忽略。
            }
        }
        Ok(())
    }

    fn dispatch(&mut self, out: &mut Vec<Output>) {
        // 规范：派发时先把 id 缓冲落入 last-event-id（即使本块没有 data）。
        self.last_event_id = self.id_buf.clone();
        if self.data.is_empty() {
            self.event_type = None;
            return;
        }
        // 去掉每行追加的最后一个 '\n'。data 字段在写入前已逐行校验 UTF-8，
        // 这里的转换不会失败。
        self.data.pop();
        let data = String::from_utf8(std::mem::take(&mut self.data))
            .expect("data buffer validated as UTF-8 on append");
        let event = self.event_type.take().unwrap_or_else(|| "message".to_string());
        out.push(Output::Event(Event {
            id: self.last_event_id.clone(),
            event,
            data,
        }));
    }
}
