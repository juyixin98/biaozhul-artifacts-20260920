//! 集成测试公共工具：测试客户端（手工构造握手与帧字节）。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use wsframe::frame::{encode_frame_masked, Frame, FrameReader, Opcode, PeerRole};
use wsframe::server::{ServerConfig, WsServer};

/// 启动一个临时服务端（随机端口），返回其地址。
pub fn spawn_server(cfg: ServerConfig) -> std::net::SocketAddr {
    let server = WsServer::bind_with_config("127.0.0.1:0", cfg).expect("bind test server");
    let addr = server.local_addr().unwrap();
    std::thread::spawn(move || server.run());
    addr
}

/// 默认限制（与生产默认一致）。
pub fn default_addr() -> std::net::SocketAddr {
    spawn_server(ServerConfig::default())
}

/// 测试用 WebSocket 客户端：手工发握手、逐字节/整块发帧、解析服务端帧。
pub struct TestClient {
    pub stream: TcpStream,
    reader: FrameReader,
    mask_counter: u32,
}

impl TestClient {
    /// 建立 TCP 连接并完成合法握手。
    pub fn connect(addr: std::net::SocketAddr) -> Self {
        let stream = TcpStream::connect(addr).expect("connect");
        stream.set_nodelay(true).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut client = TestClient {
            stream,
            reader: FrameReader::new(PeerRole::Server, 1 << 20),
            mask_counter: 0,
        };
        client.handshake();
        client
    }

    /// 发送一个手工构造的握手请求，返回原始响应字节。
    pub fn raw_handshake(addr: std::net::SocketAddr, request: &str) -> Vec<u8> {
        let mut stream = TcpStream::connect(addr).expect("connect");
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream.write_all(request.as_bytes()).unwrap();
        let mut buf = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            match stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        buf
    }

    fn handshake(&mut self) {
        let req = "GET / HTTP/1.1\r\n\
                   Host: 127.0.0.1\r\n\
                   Upgrade: websocket\r\n\
                   Connection: Upgrade\r\n\
                   Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
                   Sec-WebSocket-Version: 13\r\n\r\n";
        self.stream.write_all(req.as_bytes()).unwrap();
        let mut resp = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            let n = self
                .stream
                .read(&mut chunk)
                .expect("read handshake response");
            resp.extend_from_slice(&chunk[..n]);
            if resp.windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        let text = String::from_utf8_lossy(&resp);
        assert!(text.starts_with("HTTP/1.1 101"), "handshake failed: {text}");
        assert!(
            text.contains("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo="),
            "bad accept key: {text}"
        );
    }

    fn next_mask(&mut self) -> [u8; 4] {
        self.mask_counter = self.mask_counter.wrapping_add(1);
        self.mask_counter.to_be_bytes()
    }

    /// 编码一帧（带掩码，符合客户端规范）并返回原始字节。
    pub fn encode(&mut self, frame: &Frame) -> Vec<u8> {
        let mask = self.next_mask();
        encode_frame_masked(frame, Some(mask))
    }

    /// 编码并整块发送一帧。
    pub fn send(&mut self, frame: &Frame) {
        let bytes = self.encode(frame);
        self.stream.write_all(&bytes).unwrap();
    }

    /// 发送任意原始字节（用于构造非法帧）。
    pub fn send_raw(&mut self, bytes: &[u8]) {
        self.stream.write_all(bytes).unwrap();
    }

    /// 逐字节发送（验收要求：服务端必须支持逐字节到达）。
    pub fn send_bytewise(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.stream.write_all(&[b]).unwrap();
            self.stream.flush().unwrap();
        }
    }

    /// 逐字节发送，字节间间隔 `gap`（更真实地模拟慢速网络）。
    pub fn send_bytewise_slow(&mut self, bytes: &[u8], gap: Duration) {
        for &b in bytes {
            self.stream.write_all(&[b]).unwrap();
            self.stream.flush().unwrap();
            std::thread::sleep(gap);
        }
    }

    /// 读取并解析下一帧服务端帧。
    pub fn read_frame(&mut self) -> Frame {
        let mut byte = [0u8; 1];
        loop {
            let n = self.stream.read(&mut byte).expect("read frame byte");
            assert!(n == 1, "connection closed while waiting for frame");
            if let Some(frame) = self.reader.feed_byte(byte[0]).expect("parse server frame") {
                return frame;
            }
        }
    }

    /// 读取下一帧并断言它是 Close 帧，返回关闭码（无正文时为 None）。
    pub fn expect_close(&mut self) -> Option<u16> {
        let frame = self.read_frame();
        assert_eq!(
            frame.opcode,
            Opcode::Close,
            "expected close frame, got {frame:?}"
        );
        if frame.payload.len() >= 2 {
            Some(u16::from_be_bytes([frame.payload[0], frame.payload[1]]))
        } else {
            None
        }
    }
}
