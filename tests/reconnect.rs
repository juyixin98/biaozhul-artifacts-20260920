//! 端到端断线续流测试（真实 TCP）。
//!
//! 覆盖验收点：
//!
//! * 断线重连后**无遗漏**（游标之后的历史全部补回）；
//! * 允许的**重复边界**：游标对应事件可能被再投递一次（at-least-once）；
//! * 游标**过期**（超出有限历史窗口）收到明确的 `event: reset`；
//! * 游标**非法 / 超前**同样收到 reset，reason 明确；
//! * reset 后客户端以 reset 帧 id 对齐游标，后续继续无缺口；
//! * HTTP 子集：404 / 405。

mod common;

use common::{collect_events_until, collect_n, connect_raw, json_field, json_number, publish_msgs, start_server};
use std::time::Duration;

/// 重放场景：先订阅拿事件，“断线”后带最后 id 重连，期望补齐之后的事件且
/// 首个被重放的事件 id 严格 > 游标（服务端游标语义）。
#[test]
fn replay_after_disconnect_has_no_gaps() {
    let server = start_server(64);

    // 第一次连接，拿前 3 个事件。
    let (status, mut c1) = connect_raw(server.addr(), "/events", None);
    assert!(status.starts_with("HTTP/1.1 200"));
    let ids = publish_msgs(&server, 3);
    let got1 = collect_n(&mut c1, 3);
    assert_eq!(got1.iter().map(|e| e.data.clone()).collect::<Vec<_>>(),
               vec!["msg-0", "msg-1", "msg-2"]);
    let last = got1.last().unwrap().id.clone().unwrap();
    assert_eq!(last, ids[2].to_string());
    drop(c1); // 模拟断线

    // 断线期间服务端继续产生事件。
    let more = publish_msgs(&server, 4); // id 4..7

    // 用最后已知 id 重连：应按顺序拿到 4..7，无重复也无遗漏。
    let (_, mut c2) = connect_raw(server.addr(), "/events", Some(&last));
    let got2 = collect_n(&mut c2, 4);
    let got_ids: Vec<u64> = got2
        .iter()
        .map(|e| e.id.as_deref().unwrap().parse().unwrap())
        .collect();
    assert_eq!(got_ids, more, "重放+实时的 id 序列必须连续无缺口");
    assert!(got_ids.iter().all(|id| *id > ids[2]), "游标事件本身不应重复");
}

/// 允许的重复边界：客户端“以为”的最后 id 比真实进度落后一个（事件已送达但
/// 游标未及更新就断线）。此时游标对应事件会被**再投递一次**——这是合法重复。
#[test]
fn duplicate_boundary_when_cursor_lags_one() {
    let server = start_server(64);

    let (_, mut c1) = connect_raw(server.addr(), "/events", None);
    let ids = publish_msgs(&server, 3);
    let _got = collect_n(&mut c1, 3);
    drop(c1);

    // 模拟客户端只持久化到 ids[1]（尽管 ids[2] 已收到）。
    let stale_cursor = ids[1].to_string();
    let next = publish_msgs(&server, 1); // id 4

    let (_, mut c2) = connect_raw(server.addr(), "/events", Some(&stale_cursor));
    let got = collect_n(&mut c2, 2);
    let ids2: Vec<u64> = got
        .iter()
        .map(|e| e.id.as_deref().unwrap().parse().unwrap())
        .collect();
    // ids[2] 被重复投递（允许的 at-least-once 重复），其后 id 4 不缺。
    assert_eq!(ids2, vec![ids[2], next[0]], "应恰好重复一个边界事件");
}

#[test]
fn expired_cursor_returns_explicit_reset_then_live_resumes() {
    // 历史窗口只有 3，先发 8 个事件把 1..5 挤出窗口。
    let server = start_server(3);
    let all = publish_msgs(&server, 8); // 历史里只剩 6,7,8

    // 用早已过期的游标 2 重连：必须收到 reset，data 中 reason 明确。
    let (_, mut c) = connect_raw(server.addr(), "/events", Some("2"));
    let got = collect_events_until(
        &mut c,
        4,
        Duration::from_secs(5),
        |e| e.event == "reset",
    );
    let reset = got.iter().find(|e| e.event == "reset").expect("必须有 reset 事件");
    assert_eq!(json_field(&reset.data, "reason"), "cursor-expired");
    assert_eq!(json_field(&reset.data, "yourLastEventId"), "2");
    assert_eq!(json_number(&reset.data, "earliestId"), Some(all[5])); // id=6
    assert_eq!(json_number(&reset.data, "latestId"), Some(all[7])); // id=8
    assert_eq!(json_number(&reset.data, "resumeAfter"), Some(all[7]));
    // reset 帧自身的 id 对齐到 latestId，客户端应据此更新游标。
    assert_eq!(reset.id.as_deref(), Some(all[7].to_string().as_str()));

    // reset 之后，用 reset 给的 id 续上新事件：不再 reset、无缺口。
    let resume = reset.id.clone().unwrap();
    let fresh = publish_msgs(&server, 2); // id 9,10
    drop(c);
    let (_, mut c2) = connect_raw(server.addr(), "/events", Some(&resume));
    let got2 = collect_n(&mut c2, 2);
    let ids2: Vec<u64> = got2
        .iter()
        .map(|e| e.id.as_deref().unwrap().parse().unwrap())
        .collect();
    assert_eq!(ids2, fresh, "reset 对齐后必须无缝续上");
    assert!(got2.iter().all(|e| e.event != "reset"));
}

#[test]
fn non_numeric_cursor_returns_reset_invalid() {
    let server = start_server(8);
    publish_msgs(&server, 2);

    let (_, mut c) = connect_raw(server.addr(), "/events", Some("not-a-number"));
    let got = collect_events_until(&mut c, 4, Duration::from_secs(5), |e| {
        e.event == "reset"
    });
    let reset = got.iter().find(|e| e.event == "reset").unwrap();
    assert_eq!(json_field(&reset.data, "reason"), "cursor-invalid");
    assert_eq!(json_field(&reset.data, "yourLastEventId"), "not-a-number");
}

#[test]
fn empty_cursor_header_is_treated_as_invalid_reset() {
    // Last-Event-ID:（空值）不是合法数字游标。
    let server = start_server(8);
    publish_msgs(&server, 1);
    let (_, mut c) = connect_raw(server.addr(), "/events", Some(""));
    let got = collect_events_until(&mut c, 4, Duration::from_secs(5), |e| {
        e.event == "reset"
    });
    assert!(got.iter().any(|e| {
        e.event == "reset" && json_field(&e.data, "reason") == "cursor-invalid"
    }));
}

#[test]
fn cursor_beyond_latest_returns_reset_ahead() {
    let server = start_server(8);
    publish_msgs(&server, 2);
    let (_, mut c) = connect_raw(server.addr(), "/events", Some("999999"));
    let got = collect_events_until(&mut c, 4, Duration::from_secs(5), |e| {
        e.event == "reset"
    });
    let reset = got.iter().find(|e| e.event == "reset").unwrap();
    assert_eq!(json_field(&reset.data, "reason"), "cursor-ahead");
    assert_eq!(json_number(&reset.data, "latestId"), Some(2));
}

#[test]
fn connection_without_header_gets_only_live_events() {
    let server = start_server(64);
    // 连接前就有的历史事件不应被无游标连接看到。
    publish_msgs(&server, 3);

    let (_, mut c) = connect_raw(server.addr(), "/events", None);
    let fresh = publish_msgs(&server, 2);
    let got = collect_n(&mut c, 2);
    let ids: Vec<u64> = got
        .iter()
        .map(|e| e.id.as_deref().unwrap().parse().unwrap())
        .collect();
    assert_eq!(ids, fresh);
}

#[test]
fn zero_history_always_resets_for_any_cursor() {
    let server = start_server(0);
    publish_msgs(&server, 3);
    let (_, mut c) = connect_raw(server.addr(), "/events", Some("1"));
    let got = collect_events_until(&mut c, 4, Duration::from_secs(5), |e| {
        e.event == "reset"
    });
    let reset = got.iter().find(|e| e.event == "reset").unwrap();
    assert_eq!(json_field(&reset.data, "reason"), "cursor-expired");
    assert_eq!(json_number(&reset.data, "earliestId"), None);
}

#[test]
fn http_errors_for_wrong_path_and_method() {
    let server = start_server(4);

    // 错误路径 -> 404。
    let (status, _s) = connect_raw(server.addr(), "/nope", None);
    assert!(status.starts_with("HTTP/1.1 404"), "got {status}");

    // 直接用裸 socket 发 POST /events -> 405。
    use std::io::{Read, Write};
    use std::net::TcpStream;
    let mut s = TcpStream::connect(server.addr()).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    s.write_all(b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        .unwrap();
    let mut buf = Vec::new();
    let mut chunk = [0u8; 64];
    loop {
        let n = s.read(&mut chunk).unwrap();
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let head = String::from_utf8_lossy(&buf);
    assert!(head.starts_with("HTTP/1.1 405"), "got {}", head.lines().next().unwrap_or(""));
}

#[test]
fn live_events_after_replay_do_not_duplicate() {
    // 重放结束后继续挂着，新事件只投一次（实时循环游标正确推进）。
    let server = start_server(64);
    let ids = publish_msgs(&server, 5);

    let (_, mut c) = connect_raw(server.addr(), "/events", Some(&ids[1].to_string()));
    // 重放 3,4,5
    let replay = collect_n(&mut c, 3);
    assert_eq!(
        replay.iter().map(|e| e.id.clone().unwrap().parse::<u64>().unwrap()).collect::<Vec<_>>(),
        vec![ids[2], ids[3], ids[4]]
    );
    // 再来两个实时事件。
    let live = publish_msgs(&server, 2);
    let later = collect_n(&mut c, 2);
    assert_eq!(
        later.iter().map(|e| e.id.clone().unwrap().parse::<u64>().unwrap()).collect::<Vec<_>>(),
        live
    );
}
