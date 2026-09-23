//! 服务端事件编码（帧序列化）。
//!
//! 见 `decode.rs` 的对偶：解码器按 WHATWG SSE 规范解释字节，编码器则生成
//! 规范要求的字节序列，保证 `encode -> decode` 可以往返一致。
//!
//! 帧格式（字段顺序固定为 `id`、`event`、`retry`、若干 `data`，最后一个空行）：
//!
//! ```text
//! id: 42\n
//! event: tick\n
//! retry: 3000\n
//! data: first line\n
//! data: second line\n
//! \n
//! ```
//!
//! 注意几个刻意的决定：
//!
//! * 统一使用 LF 行终止符（规范允许 LF / CRLF / CR，LF 是互操作最稳的）。
//! * `data: ` 冒号后**总是**带一个空格（规范的“可选空格”取带空格形态）；
//!   这样值为空的 data 行编码为 `data: \n`，值以空格开头时也不会被吃掉
//!   多于规范允许的一个前导空格。
//! * 空 id 编码为 `id\n`（**没有冒号**），这正是规范里“清空 last event id
//!   buffer”的表达，可用于服务端显式复位客户端游标。

use crate::error::EncodeError;
use std::io::Write;

/// 一个待编码的服务端事件。
///
/// 字段为 `None` 表示该字段不写入帧。`data` 为多行时，每行输出一个独立的
/// `data: ...` 字段，解码侧会按规范用 `\n` 重新拼接。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OutEvent {
    /// `id` 字段。`Some(empty)` 会编码为裸 `id\n`（清空游标）；`None` 不输出 id 行。
    pub id: Option<String>,
    /// `event` 字段（事件类型名）。
    pub event: Option<String>,
    /// 重连等待提示（毫秒），输出为 `retry: <ms>`。
    pub retry_ms: Option<u64>,
    /// data 行；每个元素一行。空向量也会产出“空事件”（`data: \n\n`），
    /// 解码后得到空字符串 data —— 与规范中单个空 data 字段等价。
    pub data: Vec<String>,
}

impl OutEvent {
    /// 生成一个只有单行 data 的事件（便捷构造器）。
    pub fn data_line(line: impl Into<String>) -> Self {
        Self {
            data: vec![line.into()],
            ..Default::default()
        }
    }

    /// 设置 id 并返回自身（链式）。
    pub fn with_id(mut self, id: impl Into<String>) -> Self {
        self.id = Some(id.into());
        self
    }

    /// 设置 event 类型并返回自身（链式）。
    pub fn with_event(mut self, event: impl Into<String>) -> Self {
        self.event = Some(event.into());
        self
    }
}

/// 校验字段值中没有裸 CR/LF。
fn check_no_newline(field: &'static str, value: &str) -> Result<(), EncodeError> {
    if value.contains('\r') || value.contains('\n') {
        return Err(EncodeError::ValueContainsNewline { field });
    }
    Ok(())
}

/// 把一个事件编码进给定的 `Write`（可以是 `&mut Vec<u8>`，也可以是 TcpStream）。
pub fn encode_into(w: &mut dyn Write, ev: &OutEvent) -> Result<(), EncodeError> {
    if let Some(id) = &ev.id {
        check_no_newline("id", id)?;
        // 空 id 必须输出裸 `id\n` 才能复位客户端的 last event id buffer；
        // 非空 id 输出 `id: <value>\n`。
        if id.is_empty() {
            w.write_all(b"id\n")?;
        } else {
            w.write_all(b"id: ")?;
            w.write_all(id.as_bytes())?;
            w.write_all(b"\n")?;
        }
    }
    if let Some(event) = &ev.event {
        check_no_newline("event", event)?;
        w.write_all(b"event: ")?;
        w.write_all(event.as_bytes())?;
        w.write_all(b"\n")?;
    }
    if let Some(ms) = ev.retry_ms {
        w.write_all(b"retry: ")?;
        w.write_all(ms.to_string().as_bytes())?;
        w.write_all(b"\n")?;
    }
    if ev.data.is_empty() {
        // 空 data 仍产生一个 data 字段，使本事件成为“可派发事件”而非纯注释块。
        w.write_all(b"data: \n")?;
    } else {
        for line in &ev.data {
            check_no_newline("data", line)?;
            w.write_all(b"data: ")?;
            w.write_all(line.as_bytes())?;
            w.write_all(b"\n")?;
        }
    }
    // 空行：派发事件。
    w.write_all(b"\n")?;
    Ok(())
}

/// 把一个事件编码成字节向量。
pub fn encode_to_vec(ev: &OutEvent) -> Result<Vec<u8>, EncodeError> {
    let mut buf = Vec::with_capacity(64);
    encode_into(&mut buf, ev)?;
    Ok(buf)
}

/// 编码一行 SSE 注释（`: ...`）。注释没有事件语义，服务端用它做心跳保活。
///
/// 值中不允许出现裸换行；多行注释请多次调用。
pub fn encode_comment_into(w: &mut dyn Write, comment: &str) -> Result<(), EncodeError> {
    check_no_newline("comment", comment)?;
    w.write_all(b": ")?;
    w.write_all(comment.as_bytes())?;
    w.write_all(b"\n")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_full_frame() {
        let ev = OutEvent {
            id: Some("42".into()),
            event: Some("tick".into()),
            retry_ms: Some(3000),
            data: vec!["a".into(), "b".into()],
        };
        let frame = encode_to_vec(&ev).unwrap();
        assert_eq!(
            frame,
            b"id: 42\nevent: tick\nretry: 3000\ndata: a\ndata: b\n\n"
        );
    }

    #[test]
    fn empty_id_is_bare_id_field() {
        let ev = OutEvent::data_line("reset").with_id("");
        let frame = encode_to_vec(&ev).unwrap();
        // 空 id -> 裸 `id\n`（复位客户端游标），不输出 event 字段。
        assert_eq!(frame, b"id\ndata: reset\n\n");
    }
}
