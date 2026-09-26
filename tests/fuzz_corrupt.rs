//! 位翻转模糊测试：任意位损坏只能产生已知错误，或解出一段自洽的数据，
//! 绝不允许 panic。注意：载荷位被翻转为另一合法码字时会解出不同内容，
//! 这是 Huffman 无内置校验和的正常现象（不是"静默损坏"bug）。

use canonical_huffman::error::Error;
use canonical_huffman::stream::{compress_bytes, decompress_bytes, IncompletePolicy, Limits};

#[test]
fn corrupted_streams_never_panic() {
    let inputs: Vec<Vec<u8>> = vec![
        vec![],
        vec![b'x'; 1],
        vec![b'q'; 5000],
        (0..3000u32).map(|i| (i % 4) as u8).collect(),
        (0..3000u32).map(|i| (i * 7 % 256) as u8).collect(),
    ];

    let mut state = 0xABCDEF01u64;
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };

    let limits = Limits::default();
    for (idx, input) in inputs.iter().enumerate() {
        let blob = compress_bytes(input, 1024).unwrap();
        for _ in 0..100 {
            let mut corrupted = blob.clone();
            // 随机翻转 1~3 个位。
            let flips = 1 + next() % 3;
            for _ in 0..flips {
                let pos = (next() as usize) % corrupted.len();
                let bit = 1u8 << (next() % 8);
                corrupted[pos] ^= bit;
            }
            match decompress_bytes(&corrupted, &limits, IncompletePolicy::Reject) {
                Ok(out) => {
                    // 自洽解码：输出长度必须与头部声明一致（解码器已保证）。
                    assert!(out.len() <= limits.max_block_bytes as usize);
                }
                Err(e) => assert!(is_known_error(&e), "case {idx}: unexpected error {e:?}"),
            }
            // allow 策略同样不得 panic。
            let _ = decompress_bytes(&corrupted, &limits, IncompletePolicy::Allow);
        }
    }
}

fn is_known_error(e: &Error) -> bool {
    matches!(
        e,
        Error::BadMagic
            | Error::UnsupportedVersion(_)
            | Error::TruncatedHeader
            | Error::TruncatedStream
            | Error::ReservedBitsSet(_)
            | Error::InvalidLengthTable(_)
            | Error::Oversubscribed
            | Error::IncompleteCode
            | Error::LengthMismatch { .. }
            | Error::UnexpectedEndOfCode
            | Error::UndefinedCodeword
            | Error::NonZeroPadding
            | Error::TrailingData
            | Error::LimitExceeded { .. }
            | Error::OutputTooLong { .. }
    )
}
