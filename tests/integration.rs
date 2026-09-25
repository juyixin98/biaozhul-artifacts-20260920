//! minimultipart 的验收级集成测试。
//!
//! 覆盖任务要求的四类关键场景：
//! 1. **包含边界前缀的二进制内容**：正文里塞入裸 `--B`、`--B-`、`--B--`、`\r\n--B-x` 等近似边界；
//! 2. **空部件**：name 存在但 0 字节正文；
//! 3. **缺结束边界 / 截断**：finish() 必须报 `Truncated`；
//! 4. **恶意超限**：part 数、头大小、单 part 正文、总大小四类限制分别报各自的错误。
//!
//! 外加：**任意分包**（在所有长度 1..=N 上循环）下结果一致，
//! 以及**不做整段缓存**断言（喂入远超上限的正文时内部缓冲始终有界）。

use minimultipart::event::Event;
use minimultipart::{Error, Limits, MultipartReader};

/// 构造一个标准两 part 的流（第二个 part 为空）。
fn sample_stream() -> Vec<u8> {
    let mut v = Vec::new();
    v.extend_from_slice(b"--B\r\n");
    v.extend_from_slice(b"Content-Disposition: form-data; name=\"a\"\r\n\r\n");
    v.extend_from_slice(b"hello");
    v.extend_from_slice(b"\r\n--B\r\n");
    v.extend_from_slice(b"Content-Disposition: form-data; name=\"empty\"; filename=\"\"\r\n\r\n");
    // 空正文
    v.extend_from_slice(b"\r\n--B--\r\n");
    v
}

/// 按 `chunk` 大小逐块喂入并 finish，返回所有事件。
fn run_chunked(
    boundary: &[u8],
    body: &[u8],
    chunk: usize,
    limits: Limits,
) -> Result<Vec<Event>, Error> {
    let mut reader = MultipartReader::new(boundary, limits)?;
    let mut events = Vec::new();
    for piece in body.chunks(chunk.max(1)) {
        events.extend(reader.feed(piece)?);
    }
    events.extend(reader.finish()?);
    Ok(events)
}

type Parts = Vec<(String, Option<String>, Vec<u8>)>;

/// 事件序列 → (part 列表：(name, filename, body), 是否收到 End)。
fn split_parts(events: Vec<Event>) -> (Parts, bool) {
    let mut parts = Vec::new();
    let mut cur: Option<(String, Option<String>, Vec<u8>)> = None;
    let mut ended = false;
    for ev in events {
        match ev {
            Event::PartBegin(m) => cur = Some((m.name, m.filename, Vec::new())),
            Event::Body(b) => cur.as_mut().unwrap().2.extend_from_slice(&b),
            Event::PartEnd => parts.push(cur.take().unwrap()),
            Event::End => ended = true,
        }
    }
    (parts, ended)
}

#[test]
fn basic_two_parts_with_empty_one() {
    let (parts, ended) =
        split_parts(run_chunked(b"B", &sample_stream(), 3, Limits::default()).unwrap());
    assert!(ended);
    assert_eq!(parts.len(), 2);
    assert_eq!(parts[0].0, "a");
    assert_eq!(parts[0].2, b"hello");
    assert_eq!(parts[1].0, "empty");
    assert!(parts[1].2.is_empty(), "空部件正文必须为空");
}

#[test]
fn every_chunk_size_from_1_gives_identical_result() {
    let body = sample_stream();
    // 注意：Body 事件的“切分粒度”随 chunk 大小变化（这是正常的流式行为），
    // 因此比较的是重建后的 part 内容，而不是原始事件序列。
    let baseline = split_parts(run_chunked(b"B", &body, usize::MAX, Limits::default()).unwrap());
    for chunk in 1..=body.len() {
        let got = split_parts(
            run_chunked(b"B", &body, chunk, Limits::default())
                .unwrap_or_else(|e| panic!("chunk_size={chunk} 解析失败: {e:?}")),
        );
        assert_eq!(got, baseline, "chunk_size={chunk} 结果与整包喂入不一致");
    }
}

#[test]
fn filename_and_extra_headers_preserved() {
    let mut body = Vec::new();
    body.extend_from_slice(b"--BOUND\r\n");
    body.extend_from_slice(b"Content-Disposition: form-data; name=\"up\"; filename=\"x.bin\"\r\n");
    body.extend_from_slice(b"Content-Type: application/octet-stream\r\n\r\n");
    body.extend_from_slice(b"\x00\x01\x02\xff\xfe");
    body.extend_from_slice(b"\r\n--BOUND--\r\n");
    let evs = run_chunked(b"BOUND", &body, 2, Limits::default()).unwrap();
    let (parts, ended) = split_parts(evs);
    assert!(ended);
    assert_eq!(parts.len(), 1);
    assert_eq!(parts[0].0, "up");
    assert_eq!(parts[0].1.as_deref(), Some("x.bin"));
    assert_eq!(parts[0].2, vec![0x00, 0x01, 0x02, 0xff, 0xfe]);
}

/// 验收核心 1：正文里出现各种“近似边界”，全部必须按原样保留。
#[test]
fn binary_content_looking_like_boundary_is_not_misjudged() {
    // 刻意包含：
    //   --B           （裸前缀，缺前导 CRLF）
    //   --B- / --B--  （像关闭边界，但缺前导 CRLF）
    //   \r\n--B-x     （有前导 CRLF、边界也对，但后缀是 '-x' 而非 '--'/CRLF）
    //   \r\n--B-      （后缀只有一个 '-',数据恰好到此结束，必须等待而非误判）
    //   \r\n--B\rx    （后缀是 CR 但下一个不是 LF）
    let payload: Vec<u8> = vec![
        b'x', b'-', b'-', b'B', b'y', b'-', b'-', b'B', b'-', b'z', b'-', b'-', b'B', b'-', b'-',
        b'\r', b'\n', b'-', b'-', b'B', b'-', b'x', b'\r', b'\n', b'-', b'-', b'B', b'\r', b'x',
        b'q',
    ];
    let mut body = Vec::new();
    body.extend_from_slice(b"--B\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n");
    body.extend_from_slice(&payload);
    body.extend_from_slice(b"\r\n--B--\r\n");

    // 每种分包（含 1 字节！）都必须还原出完全相同的 payload。
    for chunk in 1..=body.len() {
        let (parts, ended) = split_parts(
            run_chunked(b"B", &body, chunk, Limits::default())
                .unwrap_or_else(|e| panic!("chunk={chunk}: {e:?}")),
        );
        assert!(ended, "chunk={chunk}: 未收到 End");
        assert_eq!(parts.len(), 1, "chunk={chunk}");
        assert_eq!(parts[0].2, payload, "chunk={chunk}: 近似边界被误判/吞字节");
    }
}

/// 半条假关闭边界 `\r\n--B-` 横跨在 feed 的结尾时，不能提前下结论；后续字节补齐为 `-x` 后是正文。
#[test]
fn dangling_fake_close_across_feeds_is_body() {
    let head = b"--B\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n".to_vec();
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    let mut ev = Vec::new();
    ev.extend(r.feed(&head).unwrap());
    ev.extend(r.feed(b"abc\r\n--B-").unwrap()); // 停在“像关闭边界但缺第二个 -”
    assert!(r.in_body(), "数据不足时必须继续等待，不能判定结束");
    ev.extend(r.feed(b"xMORE\r\n--B--\r\n").unwrap());
    ev.extend(r.finish().unwrap());
    let (parts, ended) = split_parts(ev);
    assert!(ended);
    assert_eq!(parts[0].2, b"abc\r\n--B-xMORE");
}

/// 验收 2：多个连续空部件。
#[test]
fn multiple_empty_parts() {
    let body = b"--B\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n\
                 \r\n--B\r\nContent-Disposition: form-data; name=\"y\"\r\n\r\n\
                 \r\n--B\r\nContent-Disposition: form-data; name=\"z\"\r\n\r\nn\
                 \r\n--B--\r\n";
    let (parts, ended) = split_parts(run_chunked(b"B", body, 1, Limits::default()).unwrap());
    assert!(ended);
    assert_eq!(parts.len(), 3);
    assert!(parts[0].2.is_empty());
    assert!(parts[1].2.is_empty());
    assert_eq!(parts[2].2, b"n");
}

/// 验收 3：缺结束边界（各种截断位置）→ finish() 必须报 Truncated。
#[test]
fn missing_close_boundary_is_truncated() {
    let full = sample_stream();
    for cut in [
        1,
        5,
        full.len() - 1, // 已到 `--B--\r`，只差最后 LF（裸挂 CR）
        full.len() - 4, // 停在 "\r\n--B"，关闭标记还没成形
        full.len() - 6, // 停在 "\r\n--B-"（半个关闭标记）
    ] {
        let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
        let _ = r.feed(&full[..cut]);
        let err = r.finish().expect_err(&format!("cut={cut} 应当报错"));
        assert_eq!(
            err,
            Error::Truncated,
            "cut={cut}: 期望 Truncated, 得到 {err:?}"
        );
    }
    // 反例：恰好停在完整关闭边界 `--B--`（其后 CRLF 按 RFC 2046 可选）必须算成功。
    let ok_cut = full.len() - 2;
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    r.feed(&full[..ok_cut]).unwrap();
    r.finish().unwrap();
}

/// 头块中途结束也是截断。
#[test]
fn truncated_in_headers() {
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    r.feed(b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n")
        .unwrap();
    assert_eq!(r.finish().unwrap_err(), Error::Truncated);
}

/// 验收 4：四类超限各自报明确错误。
#[test]
fn limit_violations_have_distinct_errors() {
    let tiny = Limits::tiny();

    // part 数超限（tiny.max_parts = 2，构造 3 个）
    let mut three = Vec::new();
    for i in 0..3 {
        three.extend_from_slice(b"--B\r\n");
        three.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"p{i}\"\r\n\r\n").as_bytes(),
        );
        three.extend_from_slice(b"v");
        three.extend_from_slice(b"\r\n");
    }
    three.extend_from_slice(b"--B--\r\n");
    assert_eq!(
        run_chunked(b"B", &three, 4, tiny).unwrap_err(),
        Error::TooManyParts
    );

    // 头过大
    let mut big_hdr = b"--B\r\nContent-Disposition: form-data; name=\"a\"; x=\"".to_vec();
    big_hdr.extend(vec![b'x'; 300]);
    big_hdr.extend_from_slice(b"\"\r\n\r\nv\r\n--B--\r\n");
    assert_eq!(
        run_chunked(b"B", &big_hdr, 16, tiny).unwrap_err(),
        Error::HeaderTooLarge
    );

    // 单 part 正文过大：只把 part 上限调小，其他上限放宽，确保触发的是 PartTooLarge
    let part_limit = Limits {
        max_parts: 8,
        max_headers_size: 4096,
        max_part_size: 10,
        max_total_size: 100_000,
    };
    let mut big_body = b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n".to_vec();
    big_body.extend(vec![b'A'; 11]);
    big_body.extend_from_slice(b"\r\n--B--\r\n");
    assert_eq!(
        run_chunked(b"B", &big_body, 3, part_limit).unwrap_err(),
        Error::PartTooLarge
    );

    // 总大小过大：只把总上限调小，part 上限放宽，确保触发 TotalTooLarge
    let total_limit = Limits {
        max_parts: 8,
        max_headers_size: 4096,
        max_part_size: 100_000,
        max_total_size: 100,
    };
    let mut over_total = Vec::new();
    over_total.extend_from_slice(b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n");
    over_total.extend(vec![b'Z'; 400]);
    over_total.extend_from_slice(b"\r\n--B--\r\n");
    assert_eq!(
        run_chunked(b"B", &over_total, 5, total_limit).unwrap_err(),
        Error::TotalTooLarge
    );
}

/// 错误类型：起始边界不对 / 分隔行损坏 / 缺 disposition / 畸形头。
#[test]
fn malformed_inputs_have_explicit_errors() {
    // 没有以 --B 开头（本实现不支持 preamble）
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    assert_eq!(
        r.feed(b"preamble\r\n--B\r\n").unwrap_err(),
        Error::MissingStartBoundary
    );

    // boundary 后是非法字符
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    assert_eq!(r.feed(b"--BX\r\n").unwrap_err(), Error::MalformedStream);

    // boundary 后是单 '-'（既不是 CRLF 也不是 --）
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    r.feed(b"--B-").unwrap();
    assert_eq!(r.feed(b"X").unwrap_err(), Error::MalformedStream);

    // 头里缺 Content-Disposition
    let body = b"--B\r\nContent-Type: text/plain\r\n\r\nv\r\n--B--\r\n";
    assert_eq!(
        run_chunked(b"B", body, 4, Limits::default()).unwrap_err(),
        Error::MissingDisposition
    );

    // 空头部块
    let body = b"--B\r\n\r\nv\r\n--B--\r\n";
    assert_eq!(
        run_chunked(b"B", body, 4, Limits::default()).unwrap_err(),
        Error::MissingDisposition
    );

    // 畸形头（无冒号）
    let body = b"--B\r\nnot-a-header\r\n\r\nv\r\n--B--\r\n";
    assert_eq!(
        run_chunked(b"B", body, 4, Limits::default()).unwrap_err(),
        Error::MalformedHeaders
    );

    // disposition 类型不是 form-data
    let body = b"--B\r\nContent-Disposition: attachment; name=\"a\"\r\n\r\nv\r\n--B--\r\n";
    assert_eq!(
        run_chunked(b"B", body, 4, Limits::default()).unwrap_err(),
        Error::MissingDisposition
    );
}

/// 关闭边界后出现 epilogue / 杂字节 → MalformedStream（本实现明确不支持）。
#[test]
fn epilogue_is_rejected() {
    let body = b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nv\r\n--B--\r\njunk";
    assert_eq!(
        run_chunked(b"B", body, 4, Limits::default()).unwrap_err(),
        Error::MalformedStream
    );
}

/// 非法 boundary 构造即错。
#[test]
fn invalid_boundary_rejected_at_construction() {
    assert_eq!(
        MultipartReader::new(b"", Limits::default()).unwrap_err(),
        Error::InvalidBoundary
    );
    let long = vec![b'a'; 71];
    assert_eq!(
        MultipartReader::new(&long, Limits::default()).unwrap_err(),
        Error::InvalidBoundary
    );
}

/// 验收 5：**无完整缓存** —— 喂入远超上限的大正文时，内部缓冲始终保持在
/// “分隔符长度 + 一个读取块”量级，而不是随已发送字节线性增长。
#[test]
fn parser_does_not_buffer_the_entire_body() {
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    let head = b"--B\r\nContent-Disposition: form-data; name=\"big\"\r\n\r\n";
    r.feed(head).unwrap();

    // 分隔符 d=\r\n--B 长 5，分类需其后 2 字节；任何时刻 pending 不超过 d.len()+1=6 字节。
    let chunk = vec![b'A'; 4096];
    let mut max_buffered = 0usize;
    let mut emitted: u64 = 0;
    for _ in 0..100 {
        let evs = r.feed(&chunk).unwrap();
        for e in &evs {
            if let Event::Body(b) = e {
                emitted += b.len() as u64;
            }
        }
        max_buffered = max_buffered.max(r.buffered_len());
        assert!(
            r.buffered_len() <= 6,
            "正文期间内部缓存应 ≤6，实际 {}",
            r.buffered_len()
        );
    }
    // 收尾
    let tail = b"\r\n--B--\r\n";
    let evs = r.feed(tail).unwrap();
    for e in &evs {
        if let Event::Body(b) = e {
            emitted += b.len() as u64;
        }
    }
    r.finish().unwrap();
    assert_eq!(emitted, 100 * 4096, "全部正文必须原样透传");
    // 409600 字节流过，而峰值缓存只有个位数 —— 证明是增量解析。
    assert!(max_buffered <= 6, "峰值缓存 {max_buffered} 超出预期");
}

/// 喂入 0 字节块必须是无害的 no-op。
#[test]
fn empty_feed_is_noop() {
    let mut r = MultipartReader::new(b"B", Limits::default()).unwrap();
    assert!(r.feed(b"").unwrap().is_empty());
    r.feed(b"--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nv\r\n--B--\r\n")
        .unwrap();
    r.finish().unwrap();
}
