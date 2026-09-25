//! 演示手写增量字节解析库的三个明确能力：
//!
//! 1. 子集（subset）：先读长度前缀，再在该前缀声明的子区间内解析；
//! 2. 长度上限：`Reader::limited` / 子解析器读到边界即报
//!    `LimitExceeded`；物理缓冲不足报 `Incomplete`；
//! 3. 显式错误类型：所有失败都是分类的 `ParseError`。
//!
//! 运行：`cargo run --example byte_parser_demo`

use ipfrag::byte_reader::{FeedBuffer, ParseError, Reader};

fn main() {
    // 构造一帧自定义的小协议：
    //   [u16 payload_len][u8 tag][payload…][u16 crc]
    let frame = {
        let mut v = Vec::new();
        v.extend_from_slice(&5u16.to_be_bytes()); // 载荷 5 字节
        v.push(0x07); // tag
        v.extend_from_slice(b"hello");
        v.extend_from_slice(&0xABCDu16.to_be_bytes());
        v
    };

    // ---------- 子集 ----------
    let mut parent = Reader::new(&frame);
    let payload_len = parent.read_u16_be().unwrap() as usize;
    let tag = parent.read_u8().unwrap();
    let mut payload_reader = parent.take_subset(payload_len).unwrap();
    let payload = payload_reader.read_bytes(payload_len).unwrap();
    assert!(payload_reader.is_empty());
    let crc = parent.read_u16_be().unwrap();
    println!("[subset]      tag={tag}, payload={payload:?}, crc={crc:#06x}");

    // 子解析器无法越界：即使父缓冲物理上还有字节
    let mut full = Reader::new(&frame);
    let mut sub = full.take_subset(2).unwrap();
    let _ = sub.read_u16_be().unwrap();
    match sub.read_u8() {
        Err(ParseError::LimitExceeded {
            requested,
            remaining,
        }) => println!(
            "[limit]       subset blocked read: requested={requested}, remaining={remaining}"
        ),
        other => panic!("unexpected: {other:?}"),
    }

    // ---------- 长度上限：逻辑上限 vs 物理结尾 ----------
    let mut limited = Reader::limited(&frame, 1);
    let _ = limited.read_u8().unwrap();
    match limited.read_u8() {
        Err(ParseError::LimitExceeded { .. }) => {
            println!("[limit]       limited() blocked a physically-present byte");
        }
        other => panic!("unexpected: {other:?}"),
    }

    let mut short = Reader::new(&frame[..1]);
    match short.read_u16_be() {
        Err(ParseError::Incomplete { needed, available }) => {
            println!("[incomplete]  physical end: needed={needed:?}, available={available}")
        }
        other => panic!("unexpected: {other:?}"),
    }

    // ---------- 增量喂入：随到随解析，不完整保留现场 ----------
    let mut stream = FeedBuffer::new();
    stream.feed(&frame[..3]); // 只来 3 字节，连 payload_len+tag 勉强够，payload 没到
    let parsed: Option<(usize, u8, Vec<u8>)> = stream
        .try_parse(|r| {
            let len = r.read_u16_be()? as usize;
            let t = r.read_u8()?;
            let body = r.read_bytes(len)?.to_vec();
            Ok((len, t, body))
        })
        .unwrap();
    assert!(parsed.is_none(), "partial feed must not consume anything");
    assert_eq!(stream.unconsumed_len(), 3, "Incomplete retains bytes");
    stream.feed(&frame[3..]);
    let (len, t, body) = stream
        .try_parse(|r| {
            let len = r.read_u16_be()? as usize;
            let t = r.read_u8()?;
            let body = r.read_bytes(len)?.to_vec();
            Ok((len, t, body))
        })
        .unwrap()
        .expect("full frame must parse");
    println!(
        "[incremental] len={len} tag={t} body={body:?}, stream left={}",
        stream.unconsumed_len() + 2
    );

    println!("OK: subset / limit / classified errors / incremental feed all demonstrated");
}
