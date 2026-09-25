//! SSE 事件编码器（服务端侧）。
//!
//! 把结构化事件编码为 SSE 字节流。多行 data 按 `\r\n` / `\r` / `\n`
//! 拆成多条 `data:` 行；id / event 名不允许包含换行或 NUL。

use std::fmt;

/// 待编码的服务端事件。字段为 `None` 时不输出对应行。
#[derive(Debug, Clone, Default)]
pub struct ServerEvent {
    pub id: Option<String>,
    pub event: Option<String>,
    pub data: String,
    /// 重连间隔（毫秒），随事件一起下发。
    pub retry: Option<u64>,
}

/// 编码错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EncodeError {
    /// id 含换行、回车或 NUL。
    InvalidId,
    /// event 名含换行、回车或 NUL。
    InvalidEventName,
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::InvalidId => write!(f, "id contains CR/LF/NUL"),
            EncodeError::InvalidEventName => write!(f, "event name contains CR/LF/NUL"),
        }
    }
}

impl std::error::Error for EncodeError {}

fn check_single_line(s: &str) -> bool {
    !s.bytes().any(|b| b == b'\r' || b == b'\n' || b == 0)
}

/// 编码一个事件为 SSE 字节块（以空行结尾）。
pub fn encode_event(ev: &ServerEvent) -> Result<Vec<u8>, EncodeError> {
    let mut out = Vec::new();
    if let Some(id) = &ev.id {
        if !check_single_line(id) {
            return Err(EncodeError::InvalidId);
        }
        out.extend_from_slice(b"id: ");
        out.extend_from_slice(id.as_bytes());
        out.push(b'\n');
    }
    if let Some(name) = &ev.event {
        if !check_single_line(name) {
            return Err(EncodeError::InvalidEventName);
        }
        out.extend_from_slice(b"event: ");
        out.extend_from_slice(name.as_bytes());
        out.push(b'\n');
    }
    if let Some(ms) = ev.retry {
        out.extend_from_slice(format!("retry: {ms}\n").as_bytes());
    }
    // data 按 \r\n / \r / \n 拆行，每行一条 data: 字段。
    for line in split_lines(&ev.data) {
        out.extend_from_slice(b"data: ");
        out.extend_from_slice(line.as_bytes());
        out.push(b'\n');
    }
    out.push(b'\n');
    Ok(out)
}

/// 编码一条注释（心跳），如 `: hb\n\n`。
pub fn encode_comment(text: &str) -> Vec<u8> {
    let clean: String = text
        .bytes()
        .map(|b| if b == b'\r' || b == b'\n' || b == 0 { ' ' } else { b as char })
        .collect();
    format!(": {clean}\n\n").into_bytes()
}

/// 按 \r\n / \r / \n 拆分；空串产生一个空行（即 `data: ` 一条）。
fn split_lines(s: &str) -> Vec<&str> {
    let bytes = s.as_bytes();
    let mut lines = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'\r' => {
                lines.push(&s[start..i]);
                i += 1;
                if i < bytes.len() && bytes[i] == b'\n' {
                    i += 1;
                }
                start = i;
            }
            b'\n' => {
                lines.push(&s[start..i]);
                i += 1;
                start = i;
            }
            _ => i += 1,
        }
    }
    // 末尾剩余部分（含末尾换行后的空串不再补一行，符合"行"的直觉：
    // "a\n" 是单行 a，而不是 a 加空行）。
    if start < bytes.len() || lines.is_empty() {
        lines.push(&s[start..]);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_lines_basic() {
        assert_eq!(split_lines(""), vec![""]);
        assert_eq!(split_lines("a"), vec!["a"]);
        assert_eq!(split_lines("a\nb"), vec!["a", "b"]);
        assert_eq!(split_lines("a\r\nb\rc\n"), vec!["a", "b", "c"]);
    }
}
