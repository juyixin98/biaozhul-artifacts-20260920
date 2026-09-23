//! 解析器自动化测试：边界跨块、二进制近似边界、空部件、缺结束边界、
//! 恶意超限、无完整缓存（任意分片结果一致）。

use mpstream::{Error, Event, Limits, MultipartParser};

const BOUNDARY: &str = "XyZ-boundary.7";

fn limits() -> Limits {
    Limits::default()
}

/// 以固定块大小喂入完整消息，收集全部事件。
fn run_chunked(msg: &[u8], chunk_size: usize) -> Result<Vec<Event>, Error> {
    let mut p = MultipartParser::new(BOUNDARY, limits())?;
    let mut events = Vec::new();
    for chunk in msg.chunks(chunk_size.max(1)) {
        events.extend(p.feed(chunk)?);
    }
    events.extend(p.finish()?);
    Ok(events)
}

/// 用自定义限制喂入完整消息。
fn run_with_limits(msg: &[u8], limits: Limits) -> Result<Vec<Event>, Error> {
    let mut p = MultipartParser::new(BOUNDARY, limits)?;
    let mut events = Vec::new();
    for chunk in msg.chunks(13) {
        events.extend(p.feed(chunk)?);
    }
    events.extend(p.finish()?);
    Ok(events)
}

/// 把事件流还原成部件列表：(headers, body)。
fn reassemble(events: &[Event]) -> Vec<(Vec<(String, String)>, Vec<u8>)> {
    let mut parts = Vec::new();
    let mut cur: Option<(Vec<(String, String)>, Vec<u8>)> = None;
    let mut done = false;
    for ev in events {
        match ev {
            Event::PartStart { headers, .. } => {
                assert!(cur.is_none(), "PartStart before previous PartEnd");
                cur = Some((headers.clone(), Vec::new()));
            }
            Event::PartData(d) => {
                assert!(!d.is_empty(), "PartData must be non-empty");
                cur.as_mut().expect("PartData outside part").1.extend(d);
            }
            Event::PartEnd { .. } => {
                parts.push(cur.take().expect("PartEnd without PartStart"));
            }
            Event::Done => done = true,
        }
    }
    assert!(cur.is_none(), "unterminated part at end of events");
    assert!(done, "event stream missing Done");
    parts
}

fn part(name: &str, filename: Option<&str>, body: &[u8]) -> Vec<u8> {
    let mut v = Vec::new();
    v.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    let mut cd = format!("Content-Disposition: form-data; name=\"{name}\"");
    if let Some(f) = filename {
        cd.push_str(&format!("; filename=\"{f}\""));
    }
    v.extend_from_slice(cd.as_bytes());
    v.extend_from_slice(b"\r\n");
    if filename.is_some() {
        v.extend_from_slice(b"Content-Type: application/octet-stream\r\n");
    }
    v.extend_from_slice(b"\r\n");
    v.extend_from_slice(body);
    v.extend_from_slice(b"\r\n");
    v
}

fn final_boundary() -> Vec<u8> {
    format!("--{BOUNDARY}--\r\n").into_bytes()
}

// ---------- 验收 1：二进制正文含边界前缀/近似边界，不得误判 ----------

#[test]
fn binary_body_with_boundary_prefixes() {
    // 构造一段“恶意”二进制正文，包含各种近似边界：
    let mut body = Vec::new();
    body.extend_from_slice(b"\r\n--XyZ-boundary."); // 分隔符前缀，缺最后一个字符
    body.extend_from_slice(b"\r\n--XyZ-boundary.7!"); // 完整分隔符但后面跟非法字节
    body.extend_from_slice(b"a\r\n--XyZ-boundary.7"); // 前面是 'a' 不是行首？其实是合法前缀…
    // 上一行其实是合法分隔符形态（\r\n--boundary 后跟什么？），补上非法后续使其成为数据：
    body.extend_from_slice(b"Z");
    body.extend_from_slice(b"--XyZ-boundary.7"); // 行中间的分隔符（前面无 CRLF）
    body.extend_from_slice(b"\r\n--XyZ"); // 短前缀
    body.extend_from_slice(&[0x00, 0xff, 0x0d, 0x0a, 0x2d, 0x2d]); // 原始二进制 + "--"
    body.extend_from_slice(b"\r\n--XyZ-boundary.7\rX"); // 分隔符后跟 \rX（非 \r\n）
    body.extend(0u8..=255); // 全字节值扫一遍
    body.extend_from_slice(b"\r\n--XyZ-boundary.7-"); // 分隔符 + 单个 '-'（不足两个）

    let mut msg = part("file", Some("evil.bin"), &body);
    msg.extend_from_slice(&final_boundary());

    // 多种块大小（含逐字节）结果都必须一致且正文逐字节相等。
    let reference = reassemble(&run_chunked(&msg, usize::MAX).unwrap());
    for chunk in [1, 2, 3, 5, 7, 13, 64, 1024] {
        let parts = reassemble(&run_chunked(&msg, chunk).unwrap());
        assert_eq!(parts, reference, "chunk size {chunk} diverged");
    }
    assert_eq!(reference.len(), 1);
    assert_eq!(reference[0].1, body, "binary body corrupted by near-boundaries");
}

// ---------- 验收 2：空部件 ----------

#[test]
fn empty_part_and_empty_headers() {
    let mut msg = Vec::new();
    msg.extend_from_slice(&part("empty", None, b"")); // 零长度正文
    msg.extend_from_slice(&part("text", None, b"hello"));
    // 一个完全没有任何头的部件：
    msg.extend_from_slice(format!("--{BOUNDARY}\r\n\r\n\r\n").as_bytes());
    msg.extend_from_slice(&final_boundary());

    let parts = reassemble(&run_chunked(&msg, 4).unwrap());
    assert_eq!(parts.len(), 3);
    assert_eq!(parts[0].1, b"");
    assert_eq!(parts[1].1, b"hello");
    assert_eq!(parts[2].1, b"");
    assert!(parts[2].0.is_empty(), "headerless part must have no headers");
}

// ---------- 验收 3：缺结束边界 ----------

#[test]
fn missing_final_boundary_is_error() {
    let msg = part("a", None, b"data"); // 没有 --boundary--
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    let mut events = Vec::new();
    for chunk in msg.chunks(7) {
        events.extend(p.feed(chunk).unwrap());
    }
    assert_eq!(p.finish(), Err(Error::MissingFinalBoundary));
}

#[test]
fn truncated_at_boundary_is_error() {
    // 输入恰好停在半个结束边界处。
    let mut msg = part("a", None, b"data");
    msg.extend_from_slice(format!("--{BOUNDARY}-").as_bytes()); // 少一个 '-'
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    p.feed(&msg).unwrap();
    assert_eq!(p.finish(), Err(Error::MissingFinalBoundary));
}

// ---------- 验收 4：恶意超限 ----------

#[test]
fn too_many_parts_rejected() {
    let mut msg = Vec::new();
    for i in 0..5 {
        msg.extend_from_slice(&part(&format!("p{i}"), None, b"x"));
    }
    msg.extend_from_slice(&final_boundary());
    let lim = Limits {
        max_parts: 2,
        ..limits()
    };
    assert_eq!(
        run_with_limits(&msg, lim),
        Err(Error::TooManyParts { limit: 2 })
    );
}

#[test]
fn oversized_header_rejected() {
    let mut msg = format!("--{BOUNDARY}\r\n").into_bytes();
    msg.extend_from_slice(b"X-Pad: ");
    msg.extend(std::iter::repeat(b'A').take(4096));
    msg.extend_from_slice(b"\r\n\r\nbody\r\n");
    msg.extend_from_slice(&final_boundary());
    let lim = Limits {
        max_header_bytes: 64,
        ..limits()
    };
    assert_eq!(
        run_with_limits(&msg, lim),
        Err(Error::HeaderTooLarge { limit: 64 })
    );
}

#[test]
fn oversized_total_rejected() {
    let mut msg = part("big", Some("big.bin"), &vec![b'Z'; 100_000]);
    msg.extend_from_slice(&final_boundary());
    let lim = Limits {
        max_total_bytes: 10_000,
        ..limits()
    };
    assert_eq!(
        run_with_limits(&msg, lim),
        Err(Error::TotalSizeExceeded { limit: 10_000 })
    );
}

#[test]
fn oversized_part_rejected() {
    let mut msg = part("big", Some("big.bin"), &vec![b'Q'; 50_000]);
    msg.extend_from_slice(&final_boundary());
    let lim = Limits {
        max_part_bytes: 1_000,
        ..limits()
    };
    assert_eq!(
        run_with_limits(&msg, lim),
        Err(Error::PartTooLarge { limit: 1_000 })
    );
}

// ---------- 验收 5：无完整缓存（任意分片等价 + 内存有界） ----------

#[test]
fn arbitrary_chunking_is_equivalent() {
    let mut body = Vec::new();
    for i in 0..2000u32 {
        body.extend_from_slice(&i.to_le_bytes());
        if i % 97 == 0 {
            body.extend_from_slice(b"\r\n--XyZ"); // 撒一些前缀
        }
    }
    let mut msg = part("f", Some("data.bin"), &body);
    msg.extend_from_slice(&part("tail", None, b"end"));
    msg.extend_from_slice(&final_boundary());

    let reference = run_chunked(&msg, usize::MAX).unwrap();
    // 确定性伪随机块大小序列。
    let mut seed = 0x1234_5678u64;
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    let mut events = Vec::new();
    let mut pos = 0;
    while pos < msg.len() {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let n = (seed >> 33) as usize % 997 + 1;
        let end = (pos + n).min(msg.len());
        events.extend(p.feed(&msg[pos..end]).unwrap());
        pos = end;
    }
    events.extend(p.finish().unwrap());
    // PartData 的分片位置随喂入块大小不同而不同，因此比较重组后的部件内容。
    assert_eq!(
        reassemble(&events),
        reassemble(&reference),
        "random chunking diverged from whole-buffer"
    );
}

#[test]
fn parser_does_not_buffer_whole_body() {
    // 喂入远大于回看窗口的正文，数据必须流式吐出，
    // 最多只保留一个分隔符长度（\r\n--boundary = 18 字节）的回看窗口。
    let chunk = vec![b'x'; 8192];
    let mut got = 0usize;
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    p.feed(format!("--{BOUNDARY}\r\n\r\n").as_bytes()).unwrap();
    for _ in 0..64 {
        for ev in p.feed(&chunk).unwrap() {
            if let Event::PartData(d) = ev {
                got += d.len();
            }
        }
    }
    let window = 4 + BOUNDARY.len(); // "\r\n--" + boundary
    assert_eq!(
        got,
        64 * 8192 - window,
        "body must stream out, retaining at most one delimiter window"
    );
    // 完整消息（任意分片）解析后，正文总字节数必须分毫不差。
    let mut msg = format!("--{BOUNDARY}\r\n\r\n").into_bytes();
    for _ in 0..64 {
        msg.extend_from_slice(&chunk);
    }
    msg.extend_from_slice(b"\r\n");
    msg.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    let parts = reassemble(&run_chunked(&msg, 1024).unwrap());
    assert_eq!(parts[0].1.len(), 64 * 8192);
}

// ---------- 其他错误类型 ----------

#[test]
fn malformed_header_rejected() {
    let mut msg = format!("--{BOUNDARY}\r\n").into_bytes();
    msg.extend_from_slice(b"this line has no colon\r\n\r\nx\r\n");
    msg.extend_from_slice(&final_boundary());
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    let r = p.feed(&msg);
    assert!(matches!(r, Err(Error::MalformedHeaders(_))), "got {r:?}");
}

#[test]
fn feed_after_done_rejected() {
    let mut msg = part("a", None, b"x");
    msg.extend_from_slice(&final_boundary());
    let mut p = MultipartParser::new(BOUNDARY, limits()).unwrap();
    let events = p.feed(&msg).unwrap();
    assert!(events.contains(&Event::Done));
    assert_eq!(p.feed(b"more"), Err(Error::FeedAfterDone));
}

#[test]
fn invalid_boundary_rejected() {
    assert!(matches!(
        MultipartParser::new("", limits()),
        Err(Error::InvalidBoundary(_))
    ));
    assert!(matches!(
        MultipartParser::new(&"x".repeat(71), limits()),
        Err(Error::InvalidBoundary(_))
    ));
    assert!(matches!(
        MultipartParser::new("bad\nboundary", limits()),
        Err(Error::InvalidBoundary(_))
    ));
}

#[test]
fn preamble_is_skipped() {
    let mut msg = b"junk preamble bytes\r\nthat must be ignored\r\n".to_vec();
    msg.extend_from_slice(&part("a", None, b"ok"));
    msg.extend_from_slice(&final_boundary());
    let parts = reassemble(&run_chunked(&msg, 3).unwrap());
    assert_eq!(parts.len(), 1);
    assert_eq!(parts[0].1, b"ok");
}
