//! 解析器验收测试：CR/LF/CRLF 跨块、空 id、注释、多行 data、retry、上限与错误。

use sse_resume::encoder::{encode_event, ServerEvent};
use sse_resume::parser::{Event, Limits, Output, ParseError, Parser};

/// 逐字节喂入，验证跨块安全性。
fn feed_bytewise(p: &mut Parser, bytes: &[u8]) -> Vec<Output> {
    let mut out = Vec::new();
    for b in bytes {
        out.extend(p.feed(&[*b]).expect("bytewise feed"));
    }
    out
}

fn events(out: Vec<Output>) -> Vec<Event> {
    out.into_iter()
        .filter_map(|o| match o {
            Output::Event(e) => Some(e),
            _ => None,
        })
        .collect()
}

#[test]
fn lf_line_endings() {
    let mut p = Parser::new();
    let out = p.feed(b"data: hello\n\n").unwrap();
    let evs = events(out);
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].data, "hello");
    assert_eq!(evs[0].event, "message");
}

#[test]
fn cr_line_endings() {
    let mut p = Parser::new();
    let out = p.feed(b"data: hello\r\r").unwrap();
    let evs = events(out);
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].data, "hello");
}

#[test]
fn crlf_line_endings() {
    let mut p = Parser::new();
    let out = p.feed(b"data: hello\r\n\r\n").unwrap();
    let evs = events(out);
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].data, "hello");
}

#[test]
fn crlf_split_across_chunks() {
    // CR 在上一块末尾、LF 在下一块开头，不得产生空行/重复派发。
    let mut p = Parser::new();
    assert!(events(p.feed(b"data: a\r").unwrap()).is_empty());
    assert!(events(p.feed(b"\n").unwrap()).is_empty());
    let out = p.feed(b"\r\n").unwrap();
    let evs = events(out);
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].data, "a");
}

#[test]
fn mixed_endings_bytewise() {
    let raw = b"id: 1\r\nevent: put\ndata: l1\ndata: l2\r\r\n: tail\n\n";
    let mut p = Parser::new();
    let evs = events(feed_bytewise(&mut p, raw));
    assert_eq!(evs.len(), 1, "bytewise: {evs:?}");
    assert_eq!(evs[0].id, "1");
    assert_eq!(evs[0].event, "put");
    assert_eq!(evs[0].data, "l1\nl2");
}

#[test]
fn comment_only_block_dispatches_nothing() {
    let mut p = Parser::new();
    let out = p.feed(b": heartbeat\n\n: another\r\n\r\n").unwrap();
    assert!(events(out).is_empty());
}

#[test]
fn multiline_data_joined_with_lf() {
    let mut p = Parser::new();
    let out = p.feed(b"data: a\ndata: b\ndata: c\n\n").unwrap();
    let evs = events(out);
    assert_eq!(evs[0].data, "a\nb\nc");
}

#[test]
fn empty_id_resets_last_event_id() {
    let mut p = Parser::new();
    events(p.feed(b"id: 42\ndata: x\n\n").unwrap());
    assert_eq!(p.last_event_id(), "42");
    // 空 id 字段：last-event-id 被重置为空串。
    let out = p.feed(b"id:\ndata: y\n\n").unwrap();
    let evs = events(out);
    assert_eq!(evs[0].id, "");
    assert_eq!(p.last_event_id(), "");
}

#[test]
fn id_persists_across_events() {
    let mut p = Parser::new();
    events(p.feed(b"id: 7\ndata: a\n\n").unwrap());
    let evs = events(p.feed(b"data: b\n\n").unwrap());
    assert_eq!(evs[0].id, "7");
}

#[test]
fn id_with_nul_is_ignored() {
    let mut p = Parser::new();
    let evs = events(p.feed(b"id: 9\nid: bad\0id\ndata: a\n\n").unwrap());
    assert_eq!(evs[0].id, "9");
}

#[test]
fn unknown_fields_ignored() {
    let mut p = Parser::new();
    let evs = events(p.feed(b"foo: bar\nnonce: 1\ndata: z\n\n").unwrap());
    assert_eq!(evs[0].data, "z");
}

#[test]
fn field_without_colon_has_empty_value() {
    let mut p = Parser::new();
    // "data" 无冒号 → data 为空串，仍会派发一个空 data 事件。
    let evs = events(p.feed(b"data\n\n").unwrap());
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].data, "");
}

#[test]
fn retry_field() {
    let mut p = Parser::new();
    let out = p.feed(b"retry: 250\n\n").unwrap();
    assert_eq!(out, vec![Output::Retry(250)]);
    // 非纯数字忽略。
    let out = p.feed(b"retry: abc\n\n").unwrap();
    assert!(out.is_empty());
    // 空值忽略。
    let out = p.feed(b"retry:\n\n").unwrap();
    assert!(out.is_empty());
}

#[test]
fn id_only_block_updates_last_event_id_without_event() {
    let mut p = Parser::new();
    let out = p.feed(b"id: 33\n\n").unwrap();
    assert!(events(out).is_empty());
    assert_eq!(p.last_event_id(), "33");
}

#[test]
fn line_too_long() {
    let mut p = Parser::with_limits(Limits {
        max_line_bytes: 8,
        ..Limits::default()
    });
    let err = p.feed(b"data: 0123456789").unwrap_err();
    assert_eq!(err, ParseError::LineTooLong { limit: 8 });
}

#[test]
fn event_too_large() {
    let mut p = Parser::with_limits(Limits {
        max_event_bytes: 10,
        ..Limits::default()
    });
    let err = p.feed(b"data: 0123456789\n").unwrap_err();
    assert_eq!(err, ParseError::EventTooLarge { limit: 10 });
}

#[test]
fn invalid_utf8_rejected() {
    let mut p = Parser::new();
    let err = p.feed(b"data: \xff\xfe\n\n").unwrap_err();
    assert_eq!(err, ParseError::InvalidUtf8);
}

#[test]
fn encoder_roundtrip() {
    let ev = ServerEvent {
        id: Some("7".into()),
        event: Some("update".into()),
        data: "line1\nline2\r\nline3".into(),
        retry: Some(500),
    };
    let bytes = encode_event(&ev).unwrap();
    let mut p = Parser::new();
    let out = feed_bytewise(&mut p, &bytes);
    assert!(out.contains(&Output::Retry(500)));
    let evs = events(out);
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].id, "7");
    assert_eq!(evs[0].event, "update");
    assert_eq!(evs[0].data, "line1\nline2\nline3");
}
