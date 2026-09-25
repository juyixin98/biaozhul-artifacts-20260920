//! 极简 HTTP/1.1 请求头读取（仅服务端测试用途，非通用 HTTP 实现）。
//!
//! 支持：单个请求行 + 若干头字段 + 空行；`Content-Length` 必须存在；
//! 拒绝 `Transfer-Encoding`、非 HTTP/1.1、畸形头；头块有字节上限。

use std::io::Read;

/// 解析出的请求头信息。
#[derive(Debug)]
pub struct RequestHead {
    pub content_length: u64,
    /// 原始（未解析）的 Content-Type 值，供 multipart boundary 解析。
    pub content_type: Option<String>,
}

/// 头读取/解析错误。携带建议的 HTTP 状态码。
#[derive(Debug)]
pub struct HeadError {
    pub status: u16,
    pub message: &'static str,
}

impl HeadError {
    fn bad(msg: &'static str) -> Self {
        HeadError {
            status: 400,
            message: msg,
        }
    }
}

/// 从流中逐字节读取直到 `\r\n\r\n` 的整个头块并解析。
///
/// 逐字节读取只用于**头块**（很小、一次性）；读到空行即停，空行之后到达的字节
/// 留在 TCP 流内部缓冲里，正文阶段继续读取，不会丢字节。
pub fn read_head<R: Read>(r: &mut R, max_header_bytes: usize) -> Result<RequestHead, HeadError> {
    let mut raw: Vec<u8> = Vec::new();
    let mut one = [0u8; 1];
    loop {
        let n = r
            .read(&mut one)
            .map_err(|_| HeadError::bad("header_read_error"))?;
        if n == 0 {
            return Err(HeadError::bad("eof_before_headers"));
        }
        raw.push(one[0]);
        if raw.len() >= 4 && raw[raw.len() - 4..] == *b"\r\n\r\n" {
            break;
        }
        if raw.len() > max_header_bytes {
            return Err(HeadError {
                status: 431,
                message: "header_too_large",
            });
        }
    }

    let end = raw.len() - 4;
    let head_text =
        std::str::from_utf8(&raw[..end]).map_err(|_| HeadError::bad("header_not_ascii"))?;
    let mut lines = head_text.split("\r\n");
    let request_line = lines
        .next()
        .ok_or_else(|| HeadError::bad("empty_request"))?;
    let mut parts = request_line.split(' ');
    let method = parts
        .next()
        .ok_or_else(|| HeadError::bad("bad_request_line"))?;
    let target = parts
        .next()
        .ok_or_else(|| HeadError::bad("bad_request_line"))?;
    let version = parts
        .next()
        .ok_or_else(|| HeadError::bad("bad_request_line"))?;
    if parts.next().is_some() {
        return Err(HeadError::bad("bad_request_line"));
    }
    if method != "POST" {
        return Err(HeadError {
            status: 405,
            message: "method_not_allowed",
        });
    }
    if version != "HTTP/1.1" {
        return Err(HeadError::bad("unsupported_http_version"));
    }
    if target.len() > 2048 {
        return Err(HeadError::bad("target_too_long"));
    }

    let mut content_length: Option<u64> = None;
    let mut content_type: Option<String> = None;
    let mut transfer_encoding = false;
    for line in lines {
        let colon = line
            .bytes()
            .position(|c| c == b':')
            .ok_or_else(|| HeadError::bad("bad_header"))?;
        let name = line[..colon].trim_end();
        let value = line[colon + 1..].trim();
        if name.is_empty()
            || !name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-!#$%&'*+.^_`|~".contains(&c))
        {
            return Err(HeadError::bad("bad_header_name"));
        }
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.is_some() {
                return Err(HeadError::bad("duplicate_content_length"));
            }
            content_length = Some(
                value
                    .parse::<u64>()
                    .map_err(|_| HeadError::bad("bad_content_length"))?,
            );
        } else if name.eq_ignore_ascii_case("content-type") {
            content_type = Some(value.to_string());
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            transfer_encoding = true;
        }
    }

    if transfer_encoding {
        return Err(HeadError {
            status: 400,
            message: "transfer_encoding_unsupported",
        });
    }
    let content_length = content_length.ok_or_else(|| HeadError::bad("missing_content_length"))?;
    Ok(RequestHead {
        content_length,
        content_type,
    })
}
