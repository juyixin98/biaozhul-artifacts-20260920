//! 端到端测试公共工具：在随机端口起真实 TCP 服务、构造掩码客户端帧、
//! 逐字节发送、读取并解析服务端（未掩码）响应帧。

#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

use wsframe::frame::Limits;
use wsframe::server::{handle_connection, ServerConfig};

/// 启动一个 raw 模式（可选手shake）测试服务，返回 (连接, 端口)。
pub fn start_server(raw: bool, limits: Option<Limits>) -> (TcpStream, u16) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().unwrap().port();
    let limits = limits.unwrap_or(Limits::DEFAULT);
    thread::spawn(move || {
        let (stream, _) = listener.accept().expect("accept");
        let cfg = ServerConfig {
            addr: "127.0.0.1:0".into(),
            raw,
            limits,
            verbose: false,
        };
        // 测试不关心服务端线程的 Err（协议错误时服务端会先回 close 帧）。
        let _ = handle_connection(stream, cfg);
    });
    // 等待监听就绪后连接。
    let mut last_err = None;
    for _ in 0..50 {
        match TcpStream::connect(("127.0.0.1", port)) {
            Ok(s) => {
                s.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                s.set_nodelay(true).unwrap();
                return (s, port);
            }
            Err(e) => {
                last_err = Some(e);
                thread::sleep(Duration::from_millis(20));
            }
        }
    }
    panic!("could not connect to test server: {last_err:?}");
}

/// 构造客户端帧（带掩码）。
pub fn frame(fin: bool, opcode: u8, payload: &[u8], key: [u8; 4]) -> Vec<u8> {
    let mut out = Vec::new();
    out.push(if fin { 0x80 | opcode } else { opcode });
    match payload.len() {
        n @ 0..=125 => out.push(0x80 | n as u8),
        n @ 126..=65535 => {
            out.push(0x80 | 126);
            out.extend_from_slice(&(n as u16).to_be_bytes());
        }
        n => {
            out.push(0x80 | 127);
            out.extend_from_slice(&(n as u64).to_be_bytes());
        }
    }
    out.extend_from_slice(&key);
    for (i, b) in payload.iter().enumerate() {
        out.push(b ^ key[i % 4]);
    }
    out
}

pub fn text(fin: bool, s: &str, key: [u8; 4]) -> Vec<u8> {
    frame(fin, 0x1, s.as_bytes(), key)
}
pub fn cont(fin: bool, p: &[u8], key: [u8; 4]) -> Vec<u8> {
    frame(fin, 0x0, p, key)
}
pub fn binary(fin: bool, p: &[u8], key: [u8; 4]) -> Vec<u8> {
    frame(fin, 0x2, p, key)
}
pub fn ctrl(opcode: u8, p: &[u8], key: [u8; 4]) -> Vec<u8> {
    frame(true, opcode, p, key)
}
pub fn ping(p: &[u8], key: [u8; 4]) -> Vec<u8> {
    ctrl(0x9, p, key)
}
pub fn close(code: Option<u16>, reason: &str, key: [u8; 4]) -> Vec<u8> {
    let mut p = Vec::new();
    if let Some(c) = code {
        p.extend_from_slice(&c.to_be_bytes());
        p.extend_from_slice(reason.as_bytes());
    }
    frame(true, 0x8, &p, key)
}

/// **逐字节**发送（每发一个字节 flush），最大化覆盖增量解析路径。
pub fn send_byte_by_byte(stream: &mut TcpStream, bytes: &[u8]) {
    for &b in bytes {
        stream.write_all(&[b]).expect("write");
        stream.flush().expect("flush");
        // 不 sleep：服务端是逐字节 read 循环，TCP_NODELAY 下能及时处理。
    }
}

/// 批量发送（服务端在帧头完整时即可判定帧级非法，无需发完载荷）。
pub fn send_all(stream: &mut TcpStream, bytes: &[u8]) {
    stream.write_all(bytes).expect("write");
    stream.flush().expect("flush");
}

/// 返回带掩码客户端帧的帧头长度（2/4/10 字段长度 + 4 掩码密钥）。
pub fn header_len(payload_len: usize) -> usize {
    let field = if payload_len <= 125 {
        2
    } else if payload_len <= 65535 {
        4
    } else {
        10
    };
    field + 4
}

/// 服务端响应帧（未掩码）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerFrame {
    pub fin: bool,
    pub opcode: u8,
    pub payload: Vec<u8>,
}

/// 读取并解析一个服务端帧（阻塞直到完整帧到达）。
pub fn read_frame(stream: &mut TcpStream) -> std::io::Result<ServerFrame> {
    let mut hdr = [0u8; 2];
    read_exact_or_eof(stream, &mut hdr)?;
    let fin = hdr[0] & 0x80 != 0;
    let opcode = hdr[0] & 0x0F;
    let masked = hdr[1] & 0x80 != 0;
    assert!(!masked, "server frames must not be masked");
    let len7 = hdr[1] & 0x7F;
    let len = match len7 {
        0..=125 => len7 as usize,
        126 => {
            let mut b = [0u8; 2];
            stream.read_exact(&mut b)?;
            u16::from_be_bytes(b) as usize
        }
        127 => {
            let mut b = [0u8; 8];
            stream.read_exact(&mut b)?;
            u64::from_be_bytes(b) as usize
        }
        _ => unreachable!(),
    };
    let mut payload = vec![0u8; len];
    if len > 0 {
        stream.read_exact(&mut payload)?;
    }
    Ok(ServerFrame {
        fin,
        opcode,
        payload,
    })
}

fn read_exact_or_eof(stream: &mut TcpStream, buf: &mut [u8]) -> std::io::Result<()> {
    let mut filled = 0;
    while filled < buf.len() {
        match stream.read(&mut buf[filled..]) {
            Ok(0) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "server closed before a full frame arrived",
                ))
            }
            Ok(n) => filled += n,
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// 读取服务端帧；连接被关闭时返回 None（用于验证“服务端发完 close 即断”）。
pub fn read_frame_or_none(stream: &mut TcpStream) -> Option<ServerFrame> {
    read_frame(stream).ok()
}

/// RFC 6455 握手请求 + 读取并返回 101 响应。
pub fn do_handshake(stream: &mut TcpStream) -> String {
    let key = "dGhlIHNhbXBsZSBub25jZQ==";
    let req = format!(
        "GET /chat HTTP/1.1\r\n\
Host: 127.0.0.1\r\n\
Upgrade: websocket\r\n\
Connection: Upgrade\r\n\
Sec-WebSocket-Key: {key}\r\n\
Sec-WebSocket-Version: 13\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).unwrap();
    stream.flush().unwrap();
    let mut resp = Vec::new();
    let mut byte = [0u8; 1];
    // 读到 \r\n\r\n
    while resp.windows(4).last() != Some(b"\r\n\r\n".as_ref()) {
        stream.read_exact(&mut byte).unwrap();
        resp.push(byte[0]);
    }
    String::from_utf8(resp).unwrap()
}
