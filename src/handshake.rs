//! RFC 6455 §4 服务端打开握手（HTTP/1.1 Upgrade）。
//!
//! 只实现测试服务所需的最小子集：
//! - 请求行必须是 `GET <任意> HTTP/1.1`；
//! - 必须有 `Upgrade: websocket`、`Connection: Upgrade`（token 包含 `upgrade`）；
//! - 必须有 `Sec-WebSocket-Key`（Base64，解码后 16 字节）；
//! - 若带 `Sec-WebSocket-Version`，必须是 13。
//!
//! 不支持扩展/子协议协商，因此 RSV 位始终必须为 0。

use std::io::{BufRead, Error, ErrorKind, Result};

use crate::sha1::{base64_encode, sha1};

/// RFC 6455 §4.2.2 规定拼接到 key 后的 GUID。
pub const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// 握手请求中服务端关心的字段。
#[derive(Debug, Clone)]
pub struct Handshake {
    pub key: String,
}

fn header_value(line: &str) -> Option<&str> {
    line.find(':').map(|i| line[i + 1..].trim())
}

/// 标准 Base64 解码（握手 key 校验用，支持末尾 `=` 填充）。
fn base64_decode(s: &str) -> Option<Vec<u8>> {
    fn val(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::new();
    for chunk in bytes.chunks(4) {
        let pad = chunk.iter().filter(|&&c| c == b'=').count();
        if pad > 2 {
            return None;
        }
        let mut sextets = [0u8; 4];
        for (i, &c) in chunk.iter().enumerate() {
            sextets[i] = if c == b'=' { 0 } else { val(c)? };
        }
        let triple = (u32::from(sextets[0]) << 18)
            | (u32::from(sextets[1]) << 12)
            | (u32::from(sextets[2]) << 6)
            | u32::from(sextets[3]);
        out.push((triple >> 16) as u8);
        if pad < 2 {
            out.push((triple >> 8) as u8);
        }
        if pad == 0 {
            out.push(triple as u8);
        }
    }
    Some(out)
}

/// 从缓冲流逐行读取并校验握手请求。读到空行即结束。
pub fn read_handshake<R: BufRead>(r: &mut R) -> Result<Handshake> {
    let mut request_line = String::new();
    let n = r.read_line(&mut request_line)?;
    if n == 0 {
        return Err(Error::new(ErrorKind::UnexpectedEof, "client closed before handshake"));
    }
    let request_line = request_line.trim_end();
    let mut parts = request_line.splitn(3, ' ');
    let method = parts.next().unwrap_or("");
    let _target = parts.next().unwrap_or("");
    let version = parts.next().unwrap_or("");
    if method != "GET" || version != "HTTP/1.1" {
        return Err(Error::new(
            ErrorKind::InvalidData,
            "handshake request line must be GET ... HTTP/1.1",
        ));
    }

    let mut key: Option<String> = None;
    let mut upgrade_ok = false;
    let mut connection_ok = false;

    loop {
        let mut line = String::new();
        let n = r.read_line(&mut line)?;
        if n == 0 {
            return Err(Error::new(ErrorKind::UnexpectedEof, "handshake headers truncated"));
        }
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break; // 头部结束
        }

        let name = line.split(':').next().unwrap_or("").trim().to_ascii_lowercase();
        let value = header_value(line).unwrap_or("");
        match name.as_str() {
            "sec-websocket-key" => key = Some(value.to_string()),
            "upgrade" => upgrade_ok = value.eq_ignore_ascii_case("websocket"),
            "connection" => {
                connection_ok = value
                    .split(',')
                    .any(|t| t.trim().eq_ignore_ascii_case("upgrade"));
            }
            "sec-websocket-version" if value.trim() != "13" => {
                return Err(Error::new(
                    ErrorKind::InvalidData,
                    "only Sec-WebSocket-Version: 13 is supported",
                ));
            }
            _ => {}
        }
    }

    if !upgrade_ok || !connection_ok {
        return Err(Error::new(
            ErrorKind::InvalidData,
            "missing Upgrade: websocket / Connection: Upgrade headers",
        ));
    }
    let key = key.ok_or_else(|| Error::new(ErrorKind::InvalidData, "missing Sec-WebSocket-Key"))?;

    // §4.1：key 必须是 Base64 编码的 16 字节。
    match base64_decode(&key) {
        Some(decoded) if decoded.len() == 16 => {}
        _ => {
            return Err(Error::new(
                ErrorKind::InvalidData,
                "Sec-WebSocket-Key must be 16 bytes base64-encoded",
            ))
        }
    }

    Ok(Handshake { key })
}

/// 计算 `Sec-WebSocket-Accept`：`base64(sha1(key + GUID))`。
pub fn accept_value(key: &str) -> String {
    let mut input = String::with_capacity(key.len() + WS_GUID.len());
    input.push_str(key);
    input.push_str(WS_GUID);
    base64_encode(&sha1(input.as_bytes()))
}

/// 构造服务端 101 响应字节。
pub fn response_bytes(key: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 101 Switching Protocols\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Accept: {accept}\r\n\
         \r\n",
        accept = accept_value(key)
    )
    .into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    const SAMPLE: &str = "GET /chat HTTP/1.1\r\n\
Host: 127.0.0.1:9001\r\n\
Upgrade: websocket\r\n\
Connection: Upgrade\r\n\
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
Sec-WebSocket-Version: 13\r\n\
\r\n";

    #[test]
    fn parses_rfc_sample_request() {
        let mut cur = Cursor::new(SAMPLE.as_bytes());
        let hs = read_handshake(&mut cur).unwrap();
        assert_eq!(hs.key, "dGhlIHNhbXBsZSBub25jZQ==");
    }

    #[test]
    fn accept_matches_rfc_4_2_2_example() {
        assert_eq!(
            accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }

    #[test]
    fn rejects_missing_upgrade_and_bad_version() {
        let bad = "GET / HTTP/1.1\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n";
        let mut cur = Cursor::new(bad.as_bytes());
        assert!(read_handshake(&mut cur).is_err());

        let bad = "GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 8\r\n\r\n";
        let mut cur = Cursor::new(bad.as_bytes());
        assert!(read_handshake(&mut cur).is_err());

        let bad = "POST / HTTP/1.1\r\n\r\n";
        let mut cur = Cursor::new(bad.as_bytes());
        assert!(read_handshake(&mut cur).is_err());
    }

    #[test]
    fn base64_roundtrip() {
        for s in ["", "f", "fo", "foo", "foob", &[0u8; 16].iter().map(|_| "a").collect::<String>()] {
            let enc = base64_encode(s.as_bytes());
            assert_eq!(base64_decode(&enc).unwrap(), s.as_bytes());
        }
    }
}
