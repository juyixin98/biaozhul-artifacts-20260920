//! 断线重连集成测试：真实 TCP 连接，模拟断线后校验
//! - 无遗漏（1..=total 全部收到）；
//! - 重复只允许出现在断线边界（本实现服务端严格重放 id > last_id，重复应为 0）；
//! - 游标过期时收到明确 reset 事件并重新同步。

use std::net::TcpListener;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use sse_resume::client::{run_client, stream_once, ClientMsg, ReconnectConfig};
use sse_resume::server::{serve, ServerConfig};

/// 绑 0 端口起服务，返回 (地址, stop 开关)。
fn start_server(cfg: ServerConfig) -> (String, Arc<AtomicBool>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = Arc::clone(&stop);
    std::thread::spawn(move || serve(listener, cfg, stop2));
    (addr, stop)
}

#[test]
fn reconnect_no_loss_and_bounded_duplicates() {
    let cfg = ServerConfig {
        total: 50,
        interval_ms: 2,
        history_cap: 64, // 历史足够大：不应出现 reset
        drop_after: Some(7), // 每条连接发 7 个事件后模拟断线
        retry_ms: Some(50),
    };
    let (addr, stop) = start_server(cfg);

    let log: Arc<Mutex<Vec<ClientMsg>>> = Arc::new(Mutex::new(Vec::new()));
    let log2 = Arc::clone(&log);
    let outcome = run_client(
        &addr,
        ReconnectConfig {
            expect_total: 50,
            max_reconnects: 60,
            retry_delay_ms: 5,
        },
        &mut move |msg| log2.lock().unwrap().push(msg),
    )
    .expect("client should complete");
    stop.store(true, std::sync::atomic::Ordering::Relaxed);

    // 无遗漏：恰好收到 1..=50。
    let mut ids = outcome.received.clone();
    ids.sort_unstable();
    assert_eq!(ids, (1..=50).collect::<Vec<u64>>(), "missing events");

    // 无 reset（历史窗口足够）。
    assert_eq!(outcome.resets, 0);

    // 重复边界：服务端严格重放 id > Last-Event-ID，因此不允许任何重复；
    // 若未来允许边界重发，也只能是断线前最后一个 id——这里收紧为 0。
    assert!(
        outcome.duplicates.is_empty(),
        "unexpected duplicates: {:?}",
        outcome.duplicates
    );

    // 确实发生了断线重连（drop_after=7, total=50 → 至少 6 次重连）。
    assert!(outcome.reconnects >= 6, "reconnects: {}", outcome.reconnects);

    // 服务端确实下发了 retry 字段。
    let log = log.lock().unwrap();
    assert!(log.iter().any(|m| matches!(m, ClientMsg::Retry(50))));
}

#[test]
fn expired_cursor_yields_explicit_reset() {
    let cfg = ServerConfig {
        total: 20,
        interval_ms: 5,
        history_cap: 3, // 只保留最近 3 个事件
        drop_after: None,
        retry_ms: None,
    };
    let (addr, stop) = start_server(cfg);

    // 等全部事件生成完毕（20 * 5ms + 余量），历史里只剩 18,19,20。
    std::thread::sleep(Duration::from_millis(500));

    // 用过期游标 Last-Event-ID: 1 连接。
    let msgs: Arc<Mutex<Vec<ClientMsg>>> = Arc::new(Mutex::new(Vec::new()));
    let msgs2 = Arc::clone(&msgs);
    // 服务可能尚未就绪，重试连接。
    let mut attempt = 0;
    loop {
        let msgs3 = Arc::clone(&msgs2);
        match stream_once(&addr, Some("1"), &mut move |m| msgs3.lock().unwrap().push(m)) {
            Ok(()) => break,
            Err(e) if attempt < 50 => {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(20));
                let _ = e;
            }
            Err(e) => panic!("connect failed: {e}"),
        }
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);

    let msgs = msgs.lock().unwrap();
    // 第一条必须是明确的 reset 事件。
    assert!(
        matches!(msgs.first(), Some(ClientMsg::Reset)),
        "expected reset first, got: {:?}",
        msgs.first()
    );
    // 随后是历史中保留的事件（18,19,20），允许客户端从中恢复同步。
    let ids: Vec<u64> = msgs
        .iter()
        .filter_map(|m| match m {
            ClientMsg::Event(e) => e.id.parse().ok(),
            _ => None,
        })
        .collect();
    assert_eq!(ids, vec![18, 19, 20]);
}

#[test]
fn resume_within_history_replays_missed_only() {
    let cfg = ServerConfig {
        total: 10,
        interval_ms: 5,
        history_cap: 16,
        drop_after: None,
        retry_ms: None,
    };
    let (addr, stop) = start_server(cfg);
    std::thread::sleep(Duration::from_millis(300)); // 等 10 个事件全部生成

    // 用 Last-Event-ID: 4 连接：应只重放 5..=10，无 reset、无重复。
    let msgs: Arc<Mutex<Vec<ClientMsg>>> = Arc::new(Mutex::new(Vec::new()));
    let msgs2 = Arc::clone(&msgs);
    let mut attempt = 0;
    loop {
        let msgs3 = Arc::clone(&msgs2);
        match stream_once(&addr, Some("4"), &mut move |m| msgs3.lock().unwrap().push(m)) {
            Ok(()) => break,
            Err(_) if attempt < 50 => {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => panic!("connect failed: {e}"),
        }
    }
    stop.store(true, std::sync::atomic::Ordering::Relaxed);

    let msgs = msgs.lock().unwrap();
    assert!(!msgs.iter().any(|m| matches!(m, ClientMsg::Reset)));
    let ids: Vec<u64> = msgs
        .iter()
        .filter_map(|m| match m {
            ClientMsg::Event(e) => e.id.parse().ok(),
            _ => None,
        })
        .collect();
    assert_eq!(ids, vec![5, 6, 7, 8, 9, 10]);
}
