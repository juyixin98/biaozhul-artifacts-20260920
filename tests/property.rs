//! 随机化（确定性 PRNG）属性测试，不依赖任何第三方 crate：
//!
//! 1. 随机生成多 part 流，part 正文里大量塞入与边界相关的字节序列；
//! 2. 在**随机大小**的 chunk 下喂入解析器；
//! 3. 断言：解析成功、每个 part 字节级一致、全程内部缓冲有界（≤ delim-1）；
//! 4. 再喂纯随机噪声字节，断言解析器只可能返回 `minimultipart::Error`，绝不 panic/死循环，
//!    且内部缓冲始终有界。

use minimultipart::event::Event;
use minimultipart::{Limits, MultipartReader};

/// 极简确定性 LCG，避免引入 rand。
struct Rng(u64);
impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed)
    }
    fn next_u64(&mut self) -> u64 {
        // MMIX 常数（显式 wrapping 运算）
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

/// 从一组“与边界相关”的碎片里随机拼正文，然后净化：
/// 保留所有“假”近似边界，只破坏碎片拼接处偶然形成的**真**分隔行
/// (`\r\n--B\r\n` / `\r\n--B--`)，以及正文末尾恰好是完整 d 前缀的情况
/// （否则会与 builder 追加的 CRLF 拼成真分隔行）。
fn random_body(rng: &mut Rng, len: usize) -> Vec<u8> {
    const FRAGMENTS: &[&[u8]] = &[
        b"abc",
        b"--",
        b"--B",
        b"--B-",
        b"--B--",
        b"--Bx",
        b"\r",
        b"\n",
        b"\r\n",
        b"\r\n--B-x",
        b"\r\n--B\rx",
        b"\x00\xff\xfe",
        b" ",
        b"z9",
    ];
    let mut out = Vec::with_capacity(len);
    while out.len() < len {
        out.extend_from_slice(FRAGMENTS[rng.below(FRAGMENTS.len())]);
    }
    out.truncate(len);
    sanitize(&mut out);
    out
}

/// 破坏所有真分隔行模式（把 `--B` 后的关键后缀字节替换成 `Q`），假模式原样保留。
fn sanitize(v: &mut Vec<u8>) {
    let mut i = 0;
    while i + 7 <= v.len() {
        if &v[i..i + 5] == b"\r\n--B" {
            let nxt = v[i + 5];
            if nxt == b'-' && i + 7 <= v.len() && v[i + 6] == b'-' {
                v[i + 6] = b'Q'; // \r\n--B-Q
                i += 7;
                continue;
            }
            if nxt == b'\r' && i + 7 <= v.len() && v[i + 6] == b'\n' {
                v[i + 6] = b'Q'; // \r\n--B\rQ
                i += 7;
                continue;
            }
        }
        i += 1;
    }
    if v.ends_with(b"\r\n--B") {
        v.push(b'Q');
    }
}

fn build_stream(parts: &[Vec<u8>]) -> Vec<u8> {
    let mut v = Vec::new();
    for (i, data) in parts.iter().enumerate() {
        v.extend_from_slice(
            format!("--B\r\nContent-Disposition: form-data; name=\"p{i}\"\r\n\r\n").as_bytes(),
        );
        v.extend_from_slice(data);
        v.extend_from_slice(b"\r\n");
    }
    v.extend_from_slice(b"--B--\r\n");
    v
}

fn collect(events: Vec<Event>) -> Vec<Vec<u8>> {
    let mut parts: Vec<Vec<u8>> = Vec::new();
    let mut cur: Option<Vec<u8>> = None;
    for ev in events {
        match ev {
            Event::PartBegin(_) => cur = Some(Vec::new()),
            Event::Body(b) => cur.as_mut().unwrap().extend_from_slice(&b),
            Event::PartEnd => parts.push(cur.take().unwrap()),
            Event::End => {}
        }
    }
    parts
}

#[test]
fn randomized_valid_streams_random_chunking() {
    // boundary=B → full delim d=\r\n--B 长 5，分类需要其后 2 个字节。
    // 正文期 pending 上界是 d.len()+1=6：缓冲区末尾可能是“完整 d + 1 个分类字节”，
    // 第 2 个分类字节到达前无法判定真伪，必须整体保留。
    const BUF_CAP: usize = 6;
    let mut rng = Rng::new(0xC0FFEE);
    let limits = Limits {
        max_parts: 8,
        max_headers_size: 4096,
        max_part_size: 1_000_000,
        max_total_size: 4_000_000,
    };

    for iter in 0..300 {
        let n_parts = 1 + rng.below(4);
        let mut expected: Vec<Vec<u8>> = Vec::new();
        for _ in 0..n_parts {
            let len = rng.below(200); // 允许 0 长度 → 空 part
            expected.push(random_body(&mut rng, len));
        }
        let stream = build_stream(&expected);

        let mut reader = MultipartReader::new(b"B", limits).unwrap();
        let mut events = Vec::new();
        let mut pos = 0;
        while pos < stream.len() {
            let step = 1 + rng.below(17); // 每块 1..=16 字节
            let end = (pos + step).min(stream.len());
            events.extend(reader.feed(&stream[pos..end]).unwrap_or_else(|e| {
                panic!("iter={iter} pos={pos} step={step}: 合法流却报错 {e:?}")
            }));
            if reader.in_body() {
                assert!(
                    reader.buffered_len() <= BUF_CAP,
                    "iter={iter}: 正文期缓存 {} 超过上界 {BUF_CAP}",
                    reader.buffered_len()
                );
            }
            pos = end;
        }
        events.extend(reader.finish().expect("合法流 finish 不应截断"));
        let got = collect(events);
        assert_eq!(got.len(), expected.len(), "iter={iter}: part 数不符");
        for (i, (g, e)) in got.iter().zip(&expected).enumerate() {
            assert_eq!(g, e, "iter={iter} part={i}: 字节不一致");
        }
    }
}

#[test]
fn randomized_garbage_never_panics_and_stays_bounded() {
    let mut rng = Rng::new(0xBADF00D);
    let limits = Limits {
        max_parts: 64,
        max_headers_size: 1024,
        max_part_size: 100_000,
        max_total_size: 1_000_000,
    };
    // 从起始边界前缀开始，之后全是随机字节（偶尔插入边界相关字节）。
    let alphabet: &[u8] = b"\r\n-BXx01\x00\xff;:= \t\"";
    for _ in 0..50 {
        let mut reader = MultipartReader::new(b"B", limits).unwrap();
        let _ = reader.feed(b"--B");
        let mut pos = 0usize;
        let mut max_buf = 0usize;
        let mut terminated = false;
        while pos < 20_000 {
            let step = 1 + rng.below(32);
            let mut chunk = Vec::with_capacity(step);
            for _ in 0..step {
                chunk.push(alphabet[rng.below(alphabet.len())]);
            }
            match reader.feed(&chunk) {
                Ok(_) => {
                    max_buf = max_buf.max(reader.buffered_len());
                    assert!(reader.buffered_len() < 2048, "噪声输入下缓存异常增长");
                }
                Err(_) => {
                    terminated = true;
                    break;
                }
            }
            pos += step;
        }
        // 没自然报错也要能干净结束（Truncated 是预期之一），且不 panic。
        if !terminated {
            let _ = reader.finish();
        }
        assert!(max_buf < 2048);
    }
}
