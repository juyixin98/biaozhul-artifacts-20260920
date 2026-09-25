//! WebSocket 升级握手（RFC 6455 §4.2.2）。
//!
//! 只实现本项目支持的最小子集：
//! - `GET <path> HTTP/1.1`，路径不做路由，全部接受
//! - 必须包含 `Upgrade: websocket`（token 比较，大小写不敏感）
//! - `Connection` 头必须包含 `upgrade` token
//! - `Sec-WebSocket-Key` 必须是合法 Base64 且解码为 16 字节
//! - `Sec-WebSocket-Version` 必须为 `13`
//!
//! SHA-1 与 Base64 均为本模块内的手写实现（刻意不依赖 sha1/base64 crate）。

/// 从原始 HTTP 请求字节中解析出的升级请求信息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpgradeRequest {
    pub key: [u8; 16],
}

const WEBSOCKET_GUID: &[u8] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// 解析握手请求（调用方读到 `\r\n\r\n` 为止）。
/// 失败时返回 `Err(String)`，内容为 HTTP 状态行短语，供调用方回 400。
pub fn parse_upgrade(buf: &[u8]) -> Result<UpgradeRequest, String> {
    let text = std::str::from_utf8(buf).map_err(|_| "invalid utf-8 in request".to_string())?;
    let mut lines = text.split("\r\n");
    let request_line = lines.next().ok_or("empty request")?;
    let mut parts = request_line.split(' ');
    let method = parts.next().ok_or("malformed request line")?;
    let _path = parts.next().ok_or("malformed request line")?;
    let version = parts.next().ok_or("malformed request line")?;
    if method != "GET" {
        return Err("only GET is supported".into());
    }
    if version != "HTTP/1.1" {
        return Err("only HTTP/1.1 is supported".into());
    }

    let mut has_upgrade = false;
    let mut has_connection_upgrade = false;
    let mut key: Option<[u8; 16]> = None;
    let mut version_ok = false;

    for header in lines {
        if header.is_empty() {
            continue;
        }
        let Some((name, value)) = header.split_once(':') else {
            continue;
        };
        let name = name.trim();
        let value = value.trim();
        if name.eq_ignore_ascii_case("Upgrade") && value.eq_ignore_ascii_case("websocket") {
            has_upgrade = true;
        } else if name.eq_ignore_ascii_case("Connection")
            && value
                .split(',')
                .map(|t| t.trim())
                .any(|t| t.eq_ignore_ascii_case("upgrade"))
        {
            has_connection_upgrade = true;
        } else if name.eq_ignore_ascii_case("Sec-WebSocket-Key") {
            let decoded =
                base64_decode(value).map_err(|e| format!("bad Sec-WebSocket-Key: {e}"))?;
            if decoded.len() != 16 {
                return Err("Sec-WebSocket-Key must decode to 16 bytes".into());
            }
            let mut k = [0u8; 16];
            k.copy_from_slice(&decoded);
            key = Some(k);
        } else if name.eq_ignore_ascii_case("Sec-WebSocket-Version") && value.trim() == "13" {
            version_ok = true;
        }
    }

    if !has_upgrade {
        return Err("missing Upgrade: websocket".into());
    }
    if !has_connection_upgrade {
        return Err("Connection header must contain upgrade".into());
    }
    if !version_ok {
        return Err("unsupported Sec-WebSocket-Version (need 13)".into());
    }
    let key = key.ok_or("missing Sec-WebSocket-Key")?;
    Ok(UpgradeRequest { key })
}

/// 计算 `Sec-WebSocket-Accept`：Base64(SHA1(key + GUID))。
pub fn accept_key(client_key_b64: &str) -> String {
    let mut data = client_key_b64.trim().as_bytes().to_vec();
    data.extend_from_slice(WEBSOCKET_GUID);
    let digest = sha1(&data);
    base64_encode(&digest)
}

/// 构造 101 升级成功响应。
pub fn upgrade_response(accept: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 101 Switching Protocols\r\n\
         Upgrade: websocket\r\n\
         Connection: Upgrade\r\n\
         Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    .into_bytes()
}

/// 构造 400 响应。
pub fn bad_request_response(reason: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 400 Bad Request\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n\
         {reason}",
        reason.len()
    )
    .into_bytes()
}

// ---------------------------------------------------------------------------
// Base64（手写）
// ---------------------------------------------------------------------------

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn base64_encode(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[((triple >> 18) & 0x3F) as usize] as char);
        out.push(B64_ALPHABET[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64_ALPHABET[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(B64_ALPHABET[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

fn base64_decode(input: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(input.len() / 4 * 3);
    let mut accum: u32 = 0;
    let mut bits = 0u32;
    let mut padding = 0u32;
    for c in input.chars() {
        if c == '=' {
            padding += 1;
            continue;
        }
        if padding > 0 {
            return Err("data after padding".into());
        }
        let value = match c {
            'A'..='Z' => c as u32 - 'A' as u32,
            'a'..='z' => c as u32 - 'a' as u32 + 26,
            '0'..='9' => c as u32 - '0' as u32 + 52,
            '+' => 62,
            '/' => 63,
            _ => return Err(format!("invalid base64 char {c:?}")),
        };
        accum = (accum << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((accum >> bits) as u8);
            accum &= (1 << bits) - 1;
        }
    }
    if padding > 2 {
        return Err("too much padding".into());
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// SHA-1（手写，RFC 3174）
// ---------------------------------------------------------------------------

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h0: u32 = 0x67452301;
    let mut h1: u32 = 0xEFCDAB89;
    let mut h2: u32 = 0x98BADCFE;
    let mut h3: u32 = 0x10325476;
    let mut h4: u32 = 0xC3D2E1F0;

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for block in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[i * 4],
                block[i * 4 + 1],
                block[i * 4 + 2],
                block[i * 4 + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }

        let mut a = h0;
        let mut b = h1;
        let mut c = h2;
        let mut d = h3;
        let mut e = h4;

        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }

        h0 = h0.wrapping_add(a);
        h1 = h1.wrapping_add(b);
        h2 = h2.wrapping_add(c);
        h3 = h3.wrapping_add(d);
        h4 = h4.wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for (i, h) in [h0, h1, h2, h3, h4].iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&h.to_be_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha1_known_vectors() {
        // RFC 3174 测试向量
        assert_eq!(
            hex(&sha1(b"abc")),
            "a9993e364706816aba3e25717850c26c9cd0d89d"
        );
        assert_eq!(hex(&sha1(b"")), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(
            hex(&sha1(
                b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
            )),
            "84983e441c3bd26ebaae4aa1f95129e5e54670f1"
        );
    }

    #[test]
    fn base64_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
    }

    #[test]
    fn base64_roundtrip_randomish() {
        for input in [vec![], vec![0u8], vec![0, 1, 2, 3, 255, 128, 64]] {
            let enc = base64_encode(&input);
            assert_eq!(base64_decode(&enc).unwrap(), input);
        }
    }

    #[test]
    fn accept_key_rfc_example() {
        // RFC 6455 §4.2.2 示例
        let accept = accept_key("dGhlIHNhbXBsZSBub25jZQ==");
        assert_eq!(accept, "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
    }

    #[test]
    fn parses_valid_upgrade() {
        let req = "\
GET /chat HTTP/1.1\r\n\
Host: example.com\r\n\
Upgrade: websocket\r\n\
Connection: keep-alive, Upgrade\r\n\
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
Sec-WebSocket-Version: 13\r\n\r\n";
        let parsed = parse_upgrade(req.as_bytes()).unwrap();
        assert_eq!(&base64_encode(&parsed.key), "dGhlIHNhbXBsZSBub25jZQ==");
    }

    #[test]
    fn rejects_bad_version() {
        let req = "\
GET / HTTP/1.1\r\n\
Upgrade: websocket\r\n\
Connection: Upgrade\r\n\
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\
Sec-WebSocket-Version: 8\r\n\r\n";
        assert!(parse_upgrade(req.as_bytes()).is_err());
    }

    #[test]
    fn rejects_bad_key() {
        let req = "\
GET / HTTP/1.1\r\n\
Upgrade: websocket\r\n\
Connection: Upgrade\r\n\
Sec-WebSocket-Key: short\r\n\
Sec-WebSocket-Version: 13\r\n\r\n";
        assert!(parse_upgrade(req.as_bytes()).is_err());
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
}
