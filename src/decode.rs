//! SSE 增量字节解析器（本项目的核心，纯手写，不依赖任何现成协议库）。
//!
//! # 支持的 SSE 子集
//!
//! 依据 WHATWG HTML 标准的 "Server-sent events" 解析算法，明确支持：
//!
//! | 规范元素 | 支持情况 |
//! |---|---|
//! | 行终止符 LF (`\n`)、CR (`\r`)、CRLF (`\r\n`)，含**跨 TCP 块**拆分 | ✅ |
//! | 空行派发事件（dispatching the event） | ✅ |
//! | 注释行（以 `:` 开头，丢弃但可做心跳） | ✅ |
//! | `data` 字段：多行用 `\n` 拼接；末尾多余的 `\n` 派发时去掉 | ✅ |
//! | `event` 字段：设置事件类型，派发后复位为 `message` | ✅ |
//! | `id` 字段：更新 last event id；含 U+0000 的 id **忽略整行** | ✅ |
//! | 空 id（`id` 或 `id:`）：把 last event id 置为**空串**而非保留旧值 | ✅ |
//! | `retry` 字段：仅全 ASCII 数字时作为重连等待毫秒数 | ✅ |
//! | 未知字段：忽略 | ✅ |
//! | 流首 UTF-8 BOM（可跨块到达） | ✅ |
//! | 行长 / data / id 的长度上限与明确错误类型 | ✅（见 [`DecodeError`]） |
//!
//! # 不支持 / 刻意不实现
//!
//! * “以 CRLF 或 LF 结尾的行去掉一个前导 U+003A 空格”之外的字符集处理：
//!   字段值按 UTF-8 校验，非法 UTF-8 直接报 [`DecodeError::InvalidUtf8`]，
//!   不做“替换字符”容错（规范允许替换字符，但后端测试服务里早暴露更有用）。
//! * 自动重连、`EventSource` DOM 语义：那是客户端策略，本库只负责字节→事件。
//!
//! # 增量性
//!
//! [`Decoder::push`] 接受任意长度的字节片（一个 TCP 段、一个 `read()` 的结果、
//! 甚至逐字节喂入都可以），返回本次调用中**完整派发**的事件。行尾只出现一半
//! （`\r` 在本块末尾、`\n` 在下一块开头）由解析器内部状态挂起，不会重复断行。

use crate::error::{DecodeError, Limits};

/// 一个已派发的 SSE 事件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// 事件类型（`event` 字段值）；未出现 event 字段时为 `"message"`。
    pub event: String,
    /// 多行 data 拼接后、去掉最后一个尾部 `\n` 的值。
    pub data: String,
    /// 派发瞬间 last event id buffer 的快照。
    ///
    /// * 本连接从未出现 id 字段：`None`
    /// * 出现过 `id` / `id:` / `id: `（空值）：`Some("")`
    /// * 出现过非空 id：`Some("…")`
    pub id: Option<String>,
}

impl Event {
    /// 便捷断言：默认 message 类型 + 单行 data。
    pub fn message(data: impl Into<String>) -> Self {
        Self {
            event: "message".into(),
            data: data.into(),
            id: None,
        }
    }
}

/// 流首 BOM 匹配状态。
///
/// BOM 三字节 `EF BB BF` 可能被 TCP 切成三段，所以需要显式的小状态机；
/// 一旦确定“不是 BOM 前缀”，先前缓存的字节会作为普通内容重放进正常流程。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BomState {
    /// 还在流首，一个前缀字节都没见到。
    Start,
    /// 已见到 `EF`。
    SawEf,
    /// 已见到 `EF BB`。
    SawEfBb,
    /// 已确认 BOM（或确认不是 BOM），后续不再检查。
    Done,
}

/// 增量 SSE 解码器。
///
/// 一个实例对应**一条** SSE 字节流；断线重连后请新建实例（重连后服务端会
/// 重放事件，游标由调用方通过 `Last-Event-ID` 携带，不需要解码器持久化）。
pub struct Decoder {
    limits: Limits,

    // --- 行缓冲 ---
    /// 当前未终止的行（不含行终止符）。
    line: Vec<u8>,

    // --- 事件缓冲（规范的 event stream buffer 概念）---
    data: Vec<u8>,
    event_type: Option<String>,

    /// 规范中持久的 last event id buffer（解析 id 行时立即更新，跨事件保留）。
    last_id: Option<String>,
    /// 最近一次 `retry` 字段解析出的毫秒数（尚未被取走）。
    retry_pending: Option<u64>,

    /// CR 跨块挂起：见到 `\r` 时若下一字节是 `\n`，必须合成同一个行尾。
    pending_cr: bool,

    bom: BomState,
    poisoned: bool,
}

impl Decoder {
    /// 以默认 [`Limits`] 创建解码器。
    pub fn new() -> Self {
        Self::with_limits(Limits::default())
    }

    /// 以自定义上限创建解码器（测试用 [`Limits::for_test`]）。
    pub fn with_limits(limits: Limits) -> Self {
        Self {
            limits,
            line: Vec::new(),
            data: Vec::new(),
            event_type: None,
            last_id: None,
            retry_pending: None,
            pending_cr: false,
            bom: BomState::Start,
            poisoned: false,
        }
    }

    /// 取走自上次调用以来收到的 `retry` 毫秒值（没有则 `None`）。
    ///
    /// 客户端可用它更新重连退避基准；取走即清空，不会重复报告同一个值。
    pub fn take_retry(&mut self) -> Option<u64> {
        self.retry_pending.take()
    }

    /// 当前 last event id buffer（断线时以此作为 `Last-Event-ID`）。
    pub fn last_id(&self) -> Option<&str> {
        self.last_id.as_deref()
    }

    fn fail(&mut self, e: DecodeError) -> Result<Option<Event>, DecodeError> {
        self.poisoned = true;
        Err(e)
    }

    /// 喂入一段新到达的字节，返回这段字节中**完整结束**（遇到空行）的事件。
    ///
    /// 多数调用返回 0 个事件（一行还没结束 / 事件还没收齐）；这是正常的，
    /// 调用方只需在 `Ok(v)` 非空时处理事件。返回 [`DecodeError`] 后本实例毒化，
    /// 后续所有 `push`/`finish` 返回 [`DecodeError::Poisoned`]。
    pub fn push(&mut self, chunk: &[u8]) -> Result<Vec<Event>, DecodeError> {
        if self.poisoned {
            return Err(DecodeError::Poisoned);
        }
        let mut events = Vec::new();
        for &b in chunk {
            // 流首 BOM 处理只在 Start/SawEf/SawEfBb 三态介入。
            if self.bom != BomState::Done {
                match self.handle_bom_byte(b) {
                    BomOutcome::Consume => continue,
                    BomOutcome::Replay(bytes) => {
                        for rb in bytes {
                            if let Some(ev) = self.normal_byte(rb)? {
                                events.push(ev);
                            }
                        }
                        continue;
                    }
                    BomOutcome::Pass => {}
                }
            }
            if let Some(ev) = self.normal_byte(b)? {
                events.push(ev);
            }
        }
        Ok(events)
    }

    /// 流结束时调用（对端半关闭/EOF）。处理“最后一行没有行终止符、也没有空行”
    /// 的尾巴：规范要求 EOF 视同最后一个空行，满足条件仍要派发事件。
    pub fn finish(mut self) -> Result<Vec<Event>, DecodeError> {
        if self.poisoned {
            return Err(DecodeError::Poisoned);
        }
        let mut events = Vec::new();
        if !self.line.is_empty() {
            if let Some(ev) = self.end_line()? {
                events.push(ev);
            }
        }
        // EOF 派发：data 非空才产生事件（与空行派发同规则）。
        if !self.data.is_empty() {
            if let Some(ev) = self.dispatch()? {
                events.push(ev);
            }
        }
        Ok(events)
    }

    /// 处理一个已确认属于“BOM 之后正常内容”的字节。
    fn normal_byte(&mut self, b: u8) -> Result<Option<Event>, DecodeError> {
        // 上一字节是 CR：CR 本身已经终止了一行。
        if self.pending_cr {
            self.pending_cr = false;
            if b == b'\n' {
                // CRLF：LF 被吸收，不额外终止一行。
                return Ok(None);
            }
            // CR 后跟了别的字节：CR 终止的行先结算；**当前字节本身也可能是
            // CR/LF**（如连续两个 CR = 两个空行），因此必须重新走正常流程，
            // 而不能直接把它塞进新行缓冲。
            let ev = self.end_line()?;
            // 递归深度至多为 1：走到这里时 pending_cr 已被清零。
            let ev2 = self.normal_byte(b)?;
            return Ok(ev.or(ev2));
        }
        if b == b'\r' {
            // 行尾可能是 CRLF：挂起，等下一字节确认（跨块也安全）。
            let ev = self.end_line()?;
            self.pending_cr = true;
            Ok(ev)
        } else if b == b'\n' {
            Ok(self.end_line()?)
        } else {
            self.push_line_byte(b)?;
            Ok(None)
        }
    }

    fn push_line_byte(&mut self, b: u8) -> Result<(), DecodeError> {
        self.line.push(b);
        if self.line.len() > self.limits.max_line_bytes {
            return self
                .fail(DecodeError::LineTooLong {
                    len: self.line.len(),
                    limit: self.limits.max_line_bytes,
                })
                .map(|_| ());
        }
        Ok(())
    }

    /// 一行结束（遇到任意一种行终止符）：解释该行。
    fn end_line(&mut self) -> Result<Option<Event>, DecodeError> {
        if self.poisoned {
            return Err(DecodeError::Poisoned);
        }
        // 取出整行（空行 = 派发事件）。
        let line = std::mem::take(&mut self.line);
        if line.is_empty() {
            return self.dispatch();
        }
        if line[0] == b':' {
            // 注释（心跳）：整行忽略。
            return Ok(None);
        }

        // 切出字段名与原始值。
        let (name_raw, value_raw) = match line.iter().position(|&c| c == b':') {
            Some(colon) => {
                let name = &line[..colon];
                let mut val = &line[colon + 1..];
                // 规范：值只去掉**一个**前导空格。
                if val.first() == Some(&b' ') {
                    val = &val[1..];
                }
                (name, val)
            }
            None => (line.as_slice(), &b""[..]),
        };

        match name_raw {
            b"data" => {
                let value = utf8(value_raw, "data")?;
                // 规范：每个 data 字段先 append 值，再 append 一个 U+000A。
                let added = value.len() + 1;
                if self.data.len() + added > self.limits.max_data_bytes {
                    return self.fail(DecodeError::DataTooLong {
                        len: self.data.len() + added,
                        limit: self.limits.max_data_bytes,
                    });
                }
                self.data.extend_from_slice(value.as_bytes());
                self.data.push(b'\n');
            }
            b"event" => {
                let value = utf8(value_raw, "event")?;
                self.event_type = Some(value.to_owned());
            }
            b"id" => {
                let value = utf8(value_raw, "id")?;
                if value.as_bytes().contains(&0) {
                    // 规范：id 含 U+0000 时忽略**整行**（旧值保留）。
                    return Ok(None);
                }
                if value.len() > self.limits.max_id_bytes {
                    return self.fail(DecodeError::IdTooLong {
                        len: value.len(),
                        limit: self.limits.max_id_bytes,
                    });
                }
                // 规范：解析到 id 行时**立即**更新持久的 last event id buffer
                // （即使随后空行因 data 为空不派发，id 也已生效）。
                self.last_id = Some(value.to_owned());
            }
            b"retry" => {
                // 规范：值全部为 ASCII 数字时才更新重连时间，否则忽略整行。
                // 长度先做防御性限制（避免极端长数字串解析）。ASCII 数字必然是
                // 合法 UTF-8，故直接 parse。
                match std::str::from_utf8(value_raw)
                    .ok()
                    .and_then(|s| s.parse::<u64>().ok())
                {
                    Some(ms)
                        if !value_raw.is_empty()
                            && value_raw.len() <= 20
                            && value_raw.iter().all(u8::is_ascii_digit) =>
                    {
                        self.retry_pending = Some(ms);
                    }
                    _ => { /* 非法 retry：按规范忽略整行 */ }
                }
            }
            _ => {
                // 未知字段（含规范里不做处理的字段名）：忽略。
            }
        }
        Ok(None)
    }

    /// 空行到达：满足条件则派发事件，并复位事件级缓冲。
    fn dispatch(&mut self) -> Result<Option<Event>, DecodeError> {
        // 注意：last_id 在解析 id 行时已按规范立即更新，此处只复位 data/event。
        if self.data.is_empty() {
            self.event_type = None;
            return Ok(None);
        }

        // 去掉规范要求的最后一个尾部换行。
        if self.data.last() == Some(&b'\n') {
            self.data.pop();
        }
        let data_bytes = std::mem::take(&mut self.data);
        let data = String::from_utf8(data_bytes)
            .map_err(|_| DecodeError::InvalidUtf8 { field: "data" })?;

        let event = self.event_type.take().unwrap_or_else(|| "message".to_owned());
        let id = self.last_id.clone();

        Ok(Some(Event { event, data, id }))
    }

    /// BOM 小状态机。返回对当前字节的处置方式。
    fn handle_bom_byte(&mut self, b: u8) -> BomOutcome {
        match (self.bom, b) {
            (BomState::Start, 0xEF) => {
                self.bom = BomState::SawEf;
                BomOutcome::Consume
            }
            (BomState::Start, _) => {
                self.bom = BomState::Done;
                BomOutcome::Pass
            }
            (BomState::SawEf, 0xBB) => {
                self.bom = BomState::SawEfBb;
                BomOutcome::Consume
            }
            (BomState::SawEf, 0xEF) => {
                // 前一个 EF 不是 BOM 开头，但当前 EF 可能开启新 BOM 尝试：
                // 重放旧 EF，当前 EF 重新进入 SawEf。
                self.bom = BomState::SawEf;
                BomOutcome::Replay(vec![0xEF])
            }
            (BomState::SawEf, other) => {
                // 不是 BOM：旧 EF 与当前字节都按普通内容处理。
                self.bom = BomState::Done;
                BomOutcome::Replay(vec![0xEF, other])
            }
            (BomState::SawEfBb, 0xBF) => {
                // 完整 BOM，丢弃。
                self.bom = BomState::Done;
                BomOutcome::Consume
            }
            (BomState::SawEfBb, 0xEF) => {
                // EF BB 不是 BOM；当前 EF 可能是新 BOM 的起点。
                self.bom = BomState::SawEf;
                BomOutcome::Replay(vec![0xEF, 0xBB])
            }
            (BomState::SawEfBb, other) => {
                self.bom = BomState::Done;
                BomOutcome::Replay(vec![0xEF, 0xBB, other])
            }
            (BomState::Done, _) => BomOutcome::Pass,
        }
    }
}

impl Default for Decoder {
    fn default() -> Self {
        Self::new()
    }
}

/// BOM 状态机对单字节的处置。
enum BomOutcome {
    /// 当前字节被 BOM 匹配消费。
    Consume,
    /// 当前字节不是 BOM 的一部分，`vec` 里的字节要作为普通内容重放。
    Replay(Vec<u8>),
    /// BOM 早已结束，按普通字节走正常流程。
    Pass,
}

fn utf8<'a>(bytes: &'a [u8], field: &'static str) -> Result<&'a str, DecodeError> {
    std::str::from_utf8(bytes).map_err(|_| DecodeError::InvalidUtf8 { field })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn one(input: &[u8]) -> Event {
        let mut d = Decoder::new();
        let mut evs = d.push(input).unwrap();
        evs.append(&mut d.finish().unwrap());
        assert_eq!(evs.len(), 1, "期望恰好一个事件: {:?}", evs);
        evs.pop().unwrap()
    }

    #[test]
    fn parses_single_line_data_lf() {
        let ev = one(b"data: hello\n\n");
        assert_eq!(ev.event, "message");
        assert_eq!(ev.data, "hello");
    }

    #[test]
    fn parses_single_line_data_crlf() {
        let ev = one(b"data: hello\r\n\r\n");
        assert_eq!(ev.data, "hello");
    }

    #[test]
    fn parses_single_line_data_cr() {
        // 纯 CR 行终止符也是合法 SSE。
        let ev = one(b"data: hello\r\r");
        assert_eq!(ev.data, "hello");
    }

    #[test]
    fn crlf_split_across_chunks_is_one_boundary() {
        // `...hello\r` 在第一块末尾，`\n\n` 在第二块：不得产生空行重复派发。
        let mut d = Decoder::new();
        assert!(d.push(b"data: hello\r").unwrap().is_empty());
        let evs = d.push(b"\n\n").unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].data, "hello");
    }

    #[test]
    fn cr_followed_by_non_lf_terminates_line() {
        // `\rX` = 一行结束 + X 是下一行首字节。
        let ev = one(b": ping\rdata: x\r\r");
        assert_eq!(ev.data, "x");
    }

    #[test]
    fn multiline_data_joined_with_lf() {
        let ev = one(b"data: a\ndata: b\ndata: c\n\n");
        assert_eq!(ev.data, "a\nb\nc");
    }

    #[test]
    fn event_field_sets_type_then_resets() {
        let mut d = Decoder::new();
        let evs = d
            .push(b"event: update\ndata: 1\n\ndata: 2\n\n")
            .unwrap();
        assert_eq!(evs.len(), 2);
        assert_eq!(evs[0].event, "update");
        assert_eq!(evs[0].data, "1");
        // 派发后 event type 复位为 message。
        assert_eq!(evs[1].event, "message");
        assert_eq!(evs[1].data, "2");
    }

    #[test]
    fn id_is_recorded_and_persists_across_events() {
        let mut d = Decoder::new();
        let evs = d
            .push(b"id: 10\ndata: a\n\ndata: b\n\nid: 11\ndata: c\n\n")
            .unwrap();
        assert_eq!(evs.len(), 3);
        assert_eq!(evs[0].id.as_deref(), Some("10"));
        // 没有新 id 时沿用 last event id。
        assert_eq!(evs[1].id.as_deref(), Some("10"));
        assert_eq!(evs[2].id.as_deref(), Some("11"));
        assert_eq!(d.last_id(), Some("11"));
    }

    #[test]
    fn empty_id_clears_last_id_to_empty_string() {
        let mut d = Decoder::new();
        let evs = d
            .push(b"id: 9\ndata: a\n\nid\ndata: b\n\nid:\ndata: c\n\n")
            .unwrap();
        assert_eq!(evs.len(), 3);
        assert_eq!(evs[0].id.as_deref(), Some("9"));
        // 裸 `id` 与 `id:` 都把 last id 置为空串（而不是保留 "9"）。
        assert_eq!(evs[1].id.as_deref(), Some(""));
        assert_eq!(evs[2].id.as_deref(), Some(""));
    }

    #[test]
    fn id_with_nul_is_ignored_entire_line() {
        let mut d = Decoder::new();
        // 非法 id 行被忽略；旧 id "7" 保留。
        let evs = d
            .push(b"id: 7\ndata: a\n\nid: 1\x002\ndata: b\n\n")
            .unwrap();
        assert_eq!(evs.len(), 2);
        assert_eq!(evs[1].id.as_deref(), Some("7"));
    }

    #[test]
    fn comment_lines_are_ignored() {
        let mut d = Decoder::new();
        // 注释穿插、冒号后无空格的注释、纯冒号注释。
        let evs = d
            .push(b": heartbeat\n:no-space-comment\ndata: x\n:\n\n")
            .unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].data, "x");
    }

    #[test]
    fn retry_only_accepts_ascii_digits() {
        let mut d = Decoder::new();
        d.push(b"retry: 2500\n\n").unwrap();
        assert_eq!(d.take_retry(), Some(2500));
        assert_eq!(d.take_retry(), None);

        // 负数 / 带空格 / 全角数字：规范要求忽略。
        d.push(b"retry: -1\n\n").unwrap();
        assert_eq!(d.take_retry(), None);
        d.push(b"retry: 1 0\n\n").unwrap();
        assert_eq!(d.take_retry(), None);
    }

    #[test]
    fn unknown_fields_ignored() {
        let ev = one(b"foo: bar\ndata: y\n\n");
        assert_eq!(ev.data, "y");
    }

    #[test]
    fn no_colon_means_empty_value() {
        // `data` 裸字段 = 空值 data（仍追加一个换行）。
        let ev = one(b"data\n\n");
        assert_eq!(ev.data, "");
    }

    #[test]
    fn only_one_leading_space_is_removed() {
        let ev = one(b"data:    two extra spaces kept\n\n");
        assert_eq!(ev.data, "   two extra spaces kept");
    }

    #[test]
    fn blank_line_without_data_does_not_dispatch() {
        let mut d = Decoder::new();
        let evs = d.push(b": c\n\nid: 1\n\n").unwrap();
        assert!(evs.is_empty(), "无 data 的空块不应派发: {evs:?}");
    }

    #[test]
    fn eof_dispatches_unterminated_event() {
        let mut d = Decoder::new();
        assert!(d.push(b"data: tail").unwrap().is_empty());
        // EOF 时既无行尾也无空行，仍按规范派发。
        let evs = d.finish().unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].data, "tail");
    }

    #[test]
    fn byte_by_byte_feeding_matches_one_shot() {
        let frame = b"id: 1\nevent: x\ndata: line1\ndata: line2\r\n\r\n";
        let one_shot = one(frame);
        let mut d = Decoder::new();
        let mut acc = Vec::new();
        for &b in frame {
            acc.append(&mut d.push(&[b]).unwrap());
        }
        acc.append(&mut d.finish().unwrap());
        assert_eq!(acc.len(), 1);
        assert_eq!(acc[0], one_shot);
    }

    #[test]
    fn bom_at_stream_start_is_stripped() {
        let ev = one(b"\xEF\xBB\xBFdata: bom\n\n");
        assert_eq!(ev.data, "bom");
    }

    #[test]
    fn bom_split_across_three_chunks_is_stripped() {
        let mut d = Decoder::new();
        assert!(d.push(b"\xEF").unwrap().is_empty());
        assert!(d.push(b"\xBB").unwrap().is_empty());
        let evs = d.push(b"\xBFdata: bom\n\n").unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].data, "bom");
    }

    #[test]
    fn stray_ef_bytes_are_plain_content_in_event_data() {
        // 两种“非 BOM 的 EF”都不得吞字节：
        // (1) 流首 EF 后面跟 ':'（不是 BOM 的第二字节 BB），EF ':' 重放为
        //     普通行；该字段名未知 -> 忽略；
        // (2) 注释行内含 EF（非法 UTF-8 注释按规范整体忽略，不报错）。
        let mut d = Decoder::new();
        let evs = d
            .push(b"\xEF:x\ndata: ok1\n\n: \xEF\ndata: ok2\n\n")
            .unwrap();
        assert_eq!(evs.len(), 2, "{evs:?}");
        assert_eq!(evs[0].data, "ok1");
        assert_eq!(evs[1].data, "ok2");
    }

    #[test]
    fn line_too_long_is_reported() {
        let mut d = Decoder::with_limits(Limits::for_test());
        // 行长在逐字节累计到第 9 字节时超过上限 8（错误即时报出，不等到整行）。
        let err = d.push(b"data: 123456789\n").unwrap_err();
        assert!(matches!(err, DecodeError::LineTooLong { len: 9, limit: 8 }));
        // 毒化：之后喂任何字节都报错。
        assert_eq!(d.push(b"data: x").unwrap_err(), DecodeError::Poisoned);
    }

    #[test]
    fn data_too_long_is_reported() {
        // 放宽行长，只压 data 总量：每行 8 字符 + 规范追加的 \n = 9，
        // 两行合计 18 > data 上限 16。
        let lim = Limits {
            max_line_bytes: 64,
            max_data_bytes: 16,
            max_id_bytes: 64,
        };
        let mut d = Decoder::with_limits(lim);
        let err = d
            .push(b"data: 12345678\ndata: 12345678\n\n")
            .unwrap_err();
        assert!(matches!(err, DecodeError::DataTooLong { len: 18, limit: 16 }));
    }

    #[test]
    fn id_too_long_is_reported() {
        let lim = Limits {
            max_line_bytes: 64,
            max_data_bytes: 64,
            max_id_bytes: 4,
        };
        let mut d = Decoder::with_limits(lim);
        let err = d.push(b"id: abcde\ndata: x\n\n").unwrap_err();
        assert!(matches!(err, DecodeError::IdTooLong { len: 5, limit: 4 }));
    }

    #[test]
    fn invalid_utf8_in_data_value_is_reported() {
        let mut d = Decoder::new();
        let err = d.push(b"data: \xFF\xFE\n\n").unwrap_err();
        assert!(matches!(err, DecodeError::InvalidUtf8 { field: "data" }));
    }

    #[test]
    fn line_exactly_at_limit_is_accepted() {
        let lim = Limits {
            max_line_bytes: 10,
            max_data_bytes: 64,
            max_id_bytes: 64,
        };
        let mut d = Decoder::with_limits(lim);
        // "data: abcd" = 10 字节，恰好等于上限。
        let evs = d.push(b"data: abcd\n\n").unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].data, "abcd");
    }
}
