//! 端到端验收测试：真实 TCP 连接，逐字节/分块喂给服务端。
//!
//! 覆盖验收要求：
//! - 逐字节输入
//! - Ping 插入数据分片
//! - 非法连续帧（continuation 无起始、嵌套起始、控制帧分片、控制帧超长）
//! - 半个 UTF-8 字符（跨分片合法 / 末尾残缺 / 跨分片代理）
//! - 超长消息（声明超长 + 累积超长）
//! - 关闭码检查（合法码回显、非法码 1002、非 UTF-8 原因 1007、分片中关闭 1002）

mod common;

use std::time::Duration;

use common::{default_addr, spawn_server, TestClient};
use wsframe::frame::{Frame, Opcode};
use wsframe::server::ServerConfig;

fn text(fin: bool, payload: &[u8]) -> Frame {
    Frame::new(fin, Opcode::Text, payload.to_vec())
}

fn binary(fin: bool, payload: &[u8]) -> Frame {
    Frame::new(fin, Opcode::Binary, payload.to_vec())
}

fn cont(fin: bool, payload: &[u8]) -> Frame {
    Frame::new(fin, Opcode::Continuation, payload.to_vec())
}

fn ping(payload: &[u8]) -> Frame {
    Frame::complete(Opcode::Ping, payload.to_vec())
}

fn close_raw(body: &[u8]) -> Frame {
    Frame::complete(Opcode::Close, body.to_vec())
}

// ---------------------------------------------------------------------------
// 握手
// ---------------------------------------------------------------------------

#[test]
fn handshake_succeeds_and_echoes() {
    let mut c = TestClient::connect(default_addr());
    c.send(&Frame::complete(Opcode::Text, b"hello".to_vec()));
    let f = c.read_frame();
    assert_eq!(f.opcode, Opcode::Text);
    assert_eq!(f.payload, b"hello");
}

#[test]
fn handshake_rejects_bad_version_with_400() {
    let addr = default_addr();
    let req = "GET / HTTP/1.1\r\n\
               Host: x\r\n\
               Upgrade: websocket\r\n\
               Connection: Upgrade\r\n\
               Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
               Sec-WebSocket-Version: 8\r\n\r\n";
    let resp = TestClient::raw_handshake(addr, req);
    let text = String::from_utf8_lossy(&resp);
    assert!(text.starts_with("HTTP/1.1 400"), "got: {text}");
}

// ---------------------------------------------------------------------------
// 逐字节输入
// ---------------------------------------------------------------------------

#[test]
fn bytewise_input_is_reassembled() {
    let mut c = TestClient::connect(default_addr());
    let raw = c.encode(&Frame::complete(Opcode::Text, b"byte by byte".to_vec()));
    // 每次只发一个 TCP 字节，中间留 1ms
    c.send_bytewise_slow(&raw, Duration::from_millis(1));
    let f = c.read_frame();
    assert_eq!(f.payload, b"byte by byte");
}

#[test]
fn bytewise_fragmented_message_with_two_pings() {
    let mut c = TestClient::connect(default_addr());
    // 整条消息 "中A" = E4 B8 AD 41，切成 3 个数据分片
    let mut f1 = c.encode(&text(false, &[0xE4]));
    f1.extend(c.encode(&ping(b"P")));
    f1.extend(c.encode(&cont(false, &[0xB8, 0xAD])));
    f1.extend(c.encode(&ping(b"")));
    f1.extend(c.encode(&cont(true, &[0x41])));
    c.send_bytewise(&f1);

    let p1 = c.read_frame();
    assert_eq!(p1.opcode, Opcode::Pong);
    assert_eq!(p1.payload, b"P");

    let p2 = c.read_frame();
    assert_eq!(p2.opcode, Opcode::Pong);
    assert!(p2.payload.is_empty());

    let echo = c.read_frame();
    assert_eq!(echo.opcode, Opcode::Text);
    assert_eq!(echo.payload, "中A".as_bytes());
}

// ---------------------------------------------------------------------------
// Ping 插入分片（验收重点）
// ---------------------------------------------------------------------------

#[test]
fn ping_interleaves_fragmented_utf8_character() {
    let mut c = TestClient::connect(default_addr());
    // "中" 的第一个字节 E4 发出后，消息正停在半个字符上 —— 此时插入 Ping
    c.send(&text(false, &[0xE4]));
    c.send(&ping(&[b'q'; 125])); // 控制帧最大长度 125
    let pong = c.read_frame(); // Ping 的响应必须先到达
    assert_eq!(pong.opcode, Opcode::Pong);
    assert_eq!(pong.payload, vec![b'q'; 125]);

    c.send(&cont(true, &[0xB8, 0xAD]));
    let echo = c.read_frame();
    assert_eq!(echo.payload, "中".as_bytes());
}

#[test]
fn pong_is_accepted_and_ignored() {
    let mut c = TestClient::connect(default_addr());
    c.send(&Frame::complete(Opcode::Pong, b"unsolicited".to_vec()));
    c.send(&Frame::complete(Opcode::Text, b"after-pong".to_vec()));
    let f = c.read_frame();
    assert_eq!(f.payload, b"after-pong");
}

// ---------------------------------------------------------------------------
// 非法分片序列 → 1002
// ---------------------------------------------------------------------------

#[test]
fn continuation_without_start_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send(&cont(true, b"x"));
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn nested_message_start_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send(&text(false, b"a"));
    c.send(&Frame::new(false, Opcode::Binary, b"b".to_vec()));
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn fragmented_control_frame_is_1002() {
    let mut c = TestClient::connect(default_addr());
    // FIN=0 的 ping（手工构造）
    c.send_raw(&[0x09, 0x80, 0x01, 0x02, 0x03, 0x04]);
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn control_frame_over_125_is_1002() {
    let mut c = TestClient::connect(default_addr());
    // ping 声明 126 字节（16 位扩展长度）
    let mut raw = vec![0x89, 0xFE, 0x00, 0x7E];
    let key = [0xAA, 0xBB, 0xCC, 0xDD];
    raw.extend_from_slice(&key);
    raw.extend(std::iter::repeat_n(0u8, 126));
    c.send_raw(&raw);
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn unmasked_frame_is_1002() {
    let mut c = TestClient::connect(default_addr());
    // 客户端必须掩码：发一个未掩码的文本帧
    c.send_raw(&[0x81, 0x02, b'H', b'i']);
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn rsv_bits_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send_raw(&[0xF1, 0x80, 0, 0, 0, 0]); // RSV1/2/3 置位
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn reserved_opcode_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send_raw(&[0x8B, 0x80, 1, 2, 3, 4]); // opcode 0xB 保留
    assert_eq!(c.expect_close(), Some(1002));
}

// ---------------------------------------------------------------------------
// 半个 UTF-8 字符
// ---------------------------------------------------------------------------

#[test]
fn utf8_character_split_across_fragments_is_valid() {
    let mut c = TestClient::connect(default_addr());
    c.send(&text(false, &[0xE4, 0xB8]));
    c.send(&cont(true, &[0xAD]));
    let f = c.read_frame();
    assert_eq!(f.payload, "中".as_bytes());
}

#[test]
fn dangling_half_utf8_at_message_end_is_1007() {
    let mut c = TestClient::connect(default_addr());
    c.send(&text(false, &[0xE4, 0xB8]));
    c.send(&cont(true, &[])); // FIN 但字符未完成
    assert_eq!(c.expect_close(), Some(1007));
}

#[test]
fn surrogate_split_across_fragments_is_1007() {
    let mut c = TestClient::connect(default_addr());
    c.send(&text(false, &[0xED])); // 单独看无法判定
    c.send(&cont(true, &[0xA0, 0x80])); // 拼起来是 U+D800 代理
    assert_eq!(c.expect_close(), Some(1007));
}

#[test]
fn overlong_encoding_is_1007() {
    let mut c = TestClient::connect(default_addr());
    c.send(&Frame::complete(Opcode::Text, vec![0xC0, 0x80]));
    assert_eq!(c.expect_close(), Some(1007));
}

// ---------------------------------------------------------------------------
// 超长消息
// ---------------------------------------------------------------------------

#[test]
fn oversized_declared_length_is_rejected_without_buffering() {
    let mut c = TestClient::connect(default_addr());
    // 声明 2 MiB（默认单帧上限 1 MiB），使用 64 位长度 + 掩码位
    let len: u64 = 2 << 20;
    let mut raw = vec![0x81, 0xFF];
    raw.extend_from_slice(&len.to_be_bytes());
    raw.extend_from_slice(&[0x11, 0x22, 0x33, 0x44]);
    // 故意不发送载荷：服务端在解析到长度时就应拒绝
    c.send_raw(&raw);
    assert_eq!(c.expect_close(), Some(1009));
}

#[test]
fn oversized_accumulated_message_is_1009() {
    let cfg = ServerConfig {
        max_frame_payload: 1 << 20,
        max_message: 4096,
        ..Default::default()
    };
    let mut c = TestClient::connect(spawn_server(cfg));
    c.send(&binary(false, &vec![b'a'; 3000]));
    c.send(&cont(false, &vec![b'b'; 3000])); // 累积 6000 > 4096
    assert_eq!(c.expect_close(), Some(1009));
}

#[test]
fn frame_over_frame_limit_is_1009() {
    let cfg = ServerConfig {
        max_frame_payload: 2048,
        max_message: 1 << 20,
        ..Default::default()
    };
    let mut c = TestClient::connect(spawn_server(cfg));
    c.send(&Frame::complete(Opcode::Binary, vec![0u8; 3000]));
    assert_eq!(c.expect_close(), Some(1009));
}

// ---------------------------------------------------------------------------
// 关闭码
// ---------------------------------------------------------------------------

#[test]
fn close_normal_is_echoed() {
    let mut c = TestClient::connect(default_addr());
    let body = 1000u16.to_be_bytes();
    c.send(&close_raw(&body));
    assert_eq!(c.expect_close(), Some(1000));
}

#[test]
fn close_going_away_with_reason_is_echoed() {
    let mut c = TestClient::connect(default_addr());
    let mut body = 1001u16.to_be_bytes().to_vec();
    body.extend_from_slice(b"goodbye");
    c.send(&close_raw(&body));
    let f = c.read_frame();
    assert_eq!(f.opcode, Opcode::Close);
    assert_eq!(f.payload, body); // 原样回显
}

#[test]
fn close_private_code_3000_is_echoed() {
    let mut c = TestClient::connect(default_addr());
    let body = 3000u16.to_be_bytes();
    c.send(&close_raw(&body));
    assert_eq!(c.expect_close(), Some(3000));
}

#[test]
fn close_empty_body_is_echoed_empty() {
    let mut c = TestClient::connect(default_addr());
    c.send(&close_raw(b""));
    let f = c.read_frame();
    assert_eq!(f.opcode, Opcode::Close);
    assert!(f.payload.is_empty());
}

#[test]
fn close_forbidden_code_1005_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send(&close_raw(&1005u16.to_be_bytes()));
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn close_body_len_one_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send(&close_raw(&[0x03]));
    assert_eq!(c.expect_close(), Some(1002));
}

#[test]
fn close_non_utf8_reason_is_1007() {
    let mut c = TestClient::connect(default_addr());
    let mut body = 1000u16.to_be_bytes().to_vec();
    body.push(0xFF);
    c.send(&close_raw(&body));
    assert_eq!(c.expect_close(), Some(1007));
}

#[test]
fn close_during_open_fragment_is_1002() {
    let mut c = TestClient::connect(default_addr());
    c.send(&text(false, b"partial"));
    c.send(&close_raw(&1000u16.to_be_bytes()));
    assert_eq!(c.expect_close(), Some(1002));
}

// ---------------------------------------------------------------------------
// 其他
// ---------------------------------------------------------------------------

#[test]
fn binary_message_is_echoed_verbatim() {
    let mut c = TestClient::connect(default_addr());
    let data: Vec<u8> = (0u8..=255).collect();
    c.send(&Frame::complete(Opcode::Binary, data.clone()));
    let f = c.read_frame();
    assert_eq!(f.opcode, Opcode::Binary);
    assert_eq!(f.payload, data);
}

#[test]
fn empty_text_message_is_echoed() {
    let mut c = TestClient::connect(default_addr());
    c.send(&Frame::complete(Opcode::Text, vec![]));
    let f = c.read_frame();
    assert_eq!(f.opcode, Opcode::Text);
    assert!(f.payload.is_empty());
}
