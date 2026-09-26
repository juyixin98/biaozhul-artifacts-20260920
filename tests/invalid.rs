//! 非法输入与防护测试：非法回距、截断 token、输出预算、重叠复制语义。

use lzsw::error::Error;
use lzsw::{compress, Decoder};

/// 构造合法头部。
fn header(window: u16) -> Vec<u8> {
    let mut h = b"LZSW".to_vec();
    h.push(1); // version
    h.push(0); // flags
    h.extend_from_slice(&window.to_le_bytes());
    h.extend_from_slice(&130u16.to_le_bytes()); // max_match
    h.extend_from_slice(&[0; 2]); // reserved
    h
}

fn literal(out: &mut Vec<u8>, bytes: &[u8]) {
    assert!(!bytes.is_empty() && bytes.len() <= 128);
    out.push((bytes.len() - 1) as u8);
    out.extend_from_slice(bytes);
}

fn mtch(out: &mut Vec<u8>, len: usize, dist: u16) {
    assert!((3..=130).contains(&len));
    out.push(0x80 | (len - 3) as u8);
    out.extend_from_slice(&dist.to_le_bytes());
}

fn decode_all(stream: &[u8], max_output: u64) -> Result<Vec<u8>, Error> {
    let mut dec = Decoder::new(max_output);
    let mut out = Vec::new();
    dec.feed(stream, &mut out)?;
    dec.finish()?;
    Ok(out)
}

// ---- 头部校验 ----

#[test]
fn encoder_header_matches_spec() {
    // 回归：编码器曾多写 2 字节 reserved，导致解码器把头部残余当 token
    let s = compress(b"x", 4096).unwrap();
    assert_eq!(&s[..4], b"LZSW");
    assert_eq!(s.len(), lzsw::format::HEADER_LEN + 2); // header + 1 字面量 token
}

#[test]
fn bad_magic_rejected() {
    let mut s = header(4096);
    s[0] = b'X';
    assert!(matches!(decode_all(&s, 1 << 20), Err(Error::InvalidMagic)));
}

#[test]
fn bad_version_rejected() {
    let mut s = header(4096);
    s[4] = 99;
    assert!(matches!(
        decode_all(&s, 1 << 20),
        Err(Error::UnsupportedVersion(99))
    ));
}

#[test]
fn nonzero_flags_rejected() {
    let mut s = header(4096);
    s[5] = 1;
    assert!(matches!(
        decode_all(&s, 1 << 20),
        Err(Error::InvalidHeader(_))
    ));
}

#[test]
fn zero_window_rejected() {
    let s = header(0);
    assert!(matches!(
        decode_all(&s, 1 << 20),
        Err(Error::InvalidHeader(_))
    ));
}

#[test]
fn truncated_header_rejected() {
    let s = &header(4096)[..5];
    assert!(matches!(decode_all(s, 1 << 20), Err(Error::Truncated)));
}

// ---- 回距校验 ----

#[test]
fn distance_zero_rejected() {
    let mut s = header(4096);
    literal(&mut s, b"A");
    mtch(&mut s, 3, 0);
    assert!(matches!(decode_all(&s, 1 << 20), Err(Error::DistanceZero)));
}

#[test]
fn distance_beyond_window_rejected() {
    let mut s = header(256);
    // 先输出 256 字节，使 emitted >= dist，隔离出“超窗口”这一种错误
    literal(&mut s, &[b'a'; 128]);
    literal(&mut s, &[b'a'; 128]);
    mtch(&mut s, 3, 300);
    assert!(matches!(
        decode_all(&s, 1 << 20),
        Err(Error::DistanceBeyondWindow {
            distance: 300,
            window: 256
        })
    ));
}

#[test]
fn distance_before_output_start_rejected() {
    let mut s = header(4096);
    literal(&mut s, b"abc"); // 仅 3 字节输出
    mtch(&mut s, 3, 5); // 回距 5 越过输出起点
    assert!(matches!(
        decode_all(&s, 1 << 20),
        Err(Error::DistanceBeforeOutput {
            distance: 5,
            emitted: 3
        })
    ));
}

#[test]
fn distance_equal_to_emitted_is_ok() {
    // 回距恰好等于已输出字节数：引用第一个输出字节，合法
    let mut s = header(4096);
    literal(&mut s, b"xyz");
    mtch(&mut s, 3, 3);
    assert_eq!(decode_all(&s, 1 << 20).unwrap(), b"xyzxyz");
}

// ---- 截断 token ----

#[test]
fn truncated_literal_rejected() {
    let mut s = header(4096);
    s.push(4); // 声明 5 字节字面量
    s.extend_from_slice(b"ab"); // 只给 2 字节
    assert!(matches!(decode_all(&s, 1 << 20), Err(Error::Truncated)));
}

#[test]
fn truncated_match_distance_rejected() {
    let mut s = header(4096);
    literal(&mut s, b"abc");
    s.push(0x80); // 匹配 token，但回距只有 1 字节
    s.push(0x02);
    assert!(matches!(decode_all(&s, 1 << 20), Err(Error::Truncated)));
}

#[test]
fn truncated_at_control_byte_is_ok() {
    // token 边界处结束是合法流
    let mut s = header(4096);
    literal(&mut s, b"hello");
    assert_eq!(decode_all(&s, 1 << 20).unwrap(), b"hello");
}

// ---- 输出预算（压缩炸弹防护） ----

#[test]
fn output_budget_enforced() {
    let bomb = compress(&vec![b'a'; 1_000_000], 4096).unwrap();
    assert!(bomb.len() < 50_000, "sanity: highly compressible");
    assert!(matches!(
        decode_all(&bomb, 1000),
        Err(Error::OutputLimitExceeded { limit: 1000 })
    ));
}

#[test]
fn output_budget_exact_fit_ok() {
    let data = vec![b'b'; 10_000];
    let compressed = compress(&data, 4096).unwrap();
    assert_eq!(decode_all(&compressed, 10_000).unwrap(), data);
}

// ---- 重叠复制语义 ----

#[test]
fn overlapping_copy() {
    let mut s = header(4096);
    literal(&mut s, b"ab");
    mtch(&mut s, 10, 2); // len > dist：重叠复制
    assert_eq!(decode_all(&s, 1 << 20).unwrap(), b"abababababab");
}

#[test]
fn overlapping_copy_distance_one() {
    let mut s = header(4096);
    literal(&mut s, b"Q");
    mtch(&mut s, 130, 1); // 最大匹配长度 + 最小回距
    let expect = vec![b'Q'; 131];
    assert_eq!(decode_all(&s, 1 << 20).unwrap(), expect);
}
