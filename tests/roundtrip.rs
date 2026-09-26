//! 端到端与畸形输入测试。

use canonical_huffman::stream::{compress_bytes, decompress_bytes, IncompletePolicy, Limits};

fn default_limits() -> Limits {
    Limits::default()
}

#[test]
fn empty_input_roundtrip() {
    let blob = compress_bytes(&[], 1024).unwrap();
    assert_eq!(
        blob.len(),
        6 + 19,
        "empty input = prelude + one block header"
    );
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert!(out.is_empty());
}

#[test]
fn single_symbol_roundtrip() {
    for &byte in &[0u8, 7, 255] {
        let input = vec![byte; 1000];
        let blob = compress_bytes(&input, 4096).unwrap();
        let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
        assert_eq!(out, input, "single symbol {byte}");
    }
}

#[test]
fn single_symbol_one_byte() {
    let input = b"A".to_vec();
    let blob = compress_bytes(&input, 1024).unwrap();
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert_eq!(out, b"A");
}

#[test]
fn two_symbols_roundtrip() {
    let input = b"ABABABABABAB".to_vec();
    let blob = compress_bytes(&input, 1024).unwrap();
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert_eq!(out, input);
}

#[test]
fn all_byte_values_roundtrip() {
    let input: Vec<u8> = (0u32..256).map(|b| b as u8).collect();
    let blob = compress_bytes(&input, 64).unwrap(); // 强制多块
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert_eq!(out, input);
}

#[test]
fn random_roundtrip_seeded_rng() {
    // 自带确定性 LCG，避免依赖 rand  crate。
    let mut state = 0x1234_5678u64;
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };

    for trial in 0..200 {
        let len = (next() % 4096) as usize;
        let alphabet = 1 + (next() % 256) as u16;
        let input: Vec<u8> = (0..len).map(|_| (next() % alphabet as u64) as u8).collect();
        let block = 1 + (next() % 512) as usize;
        let blob = compress_bytes(&input, block).unwrap_or_else(|e| {
            panic!("trial {trial} compress failed: {e} (len={len}, block={block})")
        });
        let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject)
            .unwrap_or_else(|e| {
                panic!("trial {trial} decompress failed: {e} (len={len}, block={block})")
            });
        assert_eq!(out, input, "trial {trial} mismatch (len={len})");
    }
}

#[test]
fn equal_frequency_roundtrip() {
    let mut input = Vec::new();
    for round in 0..100u32 {
        for s in 0..256u32 {
            input.push(((s + round) & 0xff) as u8);
        }
    }
    let blob = compress_bytes(&input, 1 << 16).unwrap();
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert_eq!(out, input);
}

#[test]
fn output_is_often_larger_for_high_entropy_small_input() {
    // 不保证变小：单字节随机输入必然变大（头部开销）。
    let input = vec![0xabu8, 0x01, 0xf7, 0x33];
    let blob = compress_bytes(&input, 1024).unwrap();
    assert!(blob.len() > input.len());
    let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
    assert_eq!(out, input);
}

// -- 截断与畸形 -------------------------------------------------------------

#[test]
fn truncated_header_detected() {
    let blob = compress_bytes(b"hello world", 1024).unwrap();
    let cut = &blob[..blob.len() / 2];
    let err = decompress_bytes(cut, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert!(
        matches!(
            err,
            canonical_huffman::Error::TruncatedHeader
                | canonical_huffman::Error::TruncatedStream
                | canonical_huffman::Error::UnexpectedEndOfCode
                | canonical_huffman::Error::NonZeroPadding
        ),
        "got {err:?}"
    );
}

#[test]
fn truncated_bitstream_detected() {
    // 砍掉载荷最后一个字节的若干位/字节。
    let input: Vec<u8> = (0..5000u32).map(|i| (i % 7) as u8).collect();
    let blob = compress_bytes(&input, 4096).unwrap();
    for cut in [blob.len() - 1, blob.len() - 2] {
        let truncated = &blob[..cut];
        let err =
            decompress_bytes(truncated, &default_limits(), IncompletePolicy::Reject).unwrap_err();
        assert!(
            matches!(
                err,
                canonical_huffman::Error::TruncatedStream
                    | canonical_huffman::Error::UnexpectedEndOfCode
            ),
            "cut={cut} got {err:?}"
        );
    }
}

#[test]
fn bad_magic_detected() {
    let mut blob = compress_bytes(b"abc", 1024).unwrap();
    blob[0] ^= 0xff;
    assert_eq!(
        decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err(),
        canonical_huffman::Error::BadMagic
    );
}

#[test]
fn forged_original_len_detected() {
    let mut blob = compress_bytes(b"abcabcabc", 1024).unwrap();
    // 块头从偏移 6 开始：flags@6, original_len@7..15。
    blob[7..15].copy_from_slice(&999u64.to_be_bytes());
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert!(
        matches!(
            err,
            canonical_huffman::Error::LengthMismatch { .. }
                | canonical_huffman::Error::InvalidLengthTable(_)
        ),
        "got {err:?}"
    );
}

#[test]
fn forged_payload_bits_too_large_detected() {
    let mut blob = compress_bytes(b"abcabcabc", 1024).unwrap();
    // payload_bits @ 块头偏移 9..17（绝对偏移 15..23）。
    blob[15..23].copy_from_slice(&(1u64 << 40).to_be_bytes());
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert!(
        matches!(
            err,
            canonical_huffman::Error::InvalidLengthTable(_)
                | canonical_huffman::Error::TruncatedStream
        ),
        "got {err:?}"
    );
}

#[test]
fn forged_length_oversubscribed_rejected() {
    // 手工构造容器：前奏 + 一个多符号块，码长表过度订阅
    // (三个长度 1 的符号：Kraft=1.5)。
    let mut blob = Vec::new();
    blob.extend_from_slice(b"CHFC");
    blob.push(1); // version
    blob.push(0); // global flags
                  // block header
    blob.push(0x01); // BFINAL
    blob.extend_from_slice(&6u64.to_be_bytes()); // original_len
    blob.extend_from_slice(&6u64.to_be_bytes()); // payload_bits
    blob.extend_from_slice(&3u16.to_be_bytes()); // num_symbols
                                                 // length table: symbols 0,1,2 all length 1
    blob.extend_from_slice(&[0, 1, 1, 1, 2, 1]);
    // payload: 6 bits
    blob.extend_from_slice(&[0b01010100]);
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert_eq!(err, canonical_huffman::Error::Oversubscribed);
}

#[test]
fn forged_incomplete_table_rejected_by_default() {
    // 码长 (1,2)：Kraft=0.75，默认策略拒绝。
    let mut blob = Vec::new();
    blob.extend_from_slice(b"CHFC");
    blob.push(1);
    blob.push(0);
    blob.push(0x01);
    blob.extend_from_slice(&2u64.to_be_bytes());
    blob.extend_from_slice(&3u64.to_be_bytes());
    blob.extend_from_slice(&2u16.to_be_bytes());
    blob.extend_from_slice(&[0, 1, 1, 2]);
    blob.extend_from_slice(&[0b010_00000]); // 0 -> sym0, 10 -> sym1
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert_eq!(err, canonical_huffman::Error::IncompleteCode);

    // 允许不完整：合法位串能解，走到未定义的 11 则报错。
    let ok = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Allow).unwrap();
    assert_eq!(ok, vec![0, 1]);
}

#[test]
fn undefined_bitstring_with_allowed_incomplete_table() {
    let mut blob = Vec::new();
    blob.extend_from_slice(b"CHFC");
    blob.push(1);
    blob.push(0);
    blob.push(0x01);
    blob.extend_from_slice(&1u64.to_be_bytes());
    blob.extend_from_slice(&2u64.to_be_bytes());
    blob.extend_from_slice(&2u16.to_be_bytes());
    blob.extend_from_slice(&[0, 1, 1, 2]);
    blob.extend_from_slice(&[0b11_000000]); // 11 未定义
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Allow).unwrap_err();
    assert_eq!(err, canonical_huffman::Error::UndefinedCodeword);
}

#[test]
fn non_zero_padding_rejected() {
    let input = b"hh".to_vec(); // 单符号 h，2 位，其余 6 位为填充
    let mut blob = compress_bytes(&input, 1024).unwrap();
    let last = blob.len() - 1;
    blob[last] |= 0b0000_0001; // 污染填充位
    let err = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err();
    assert_eq!(err, canonical_huffman::Error::NonZeroPadding);
}

#[test]
fn trailing_data_after_final_block_rejected() {
    let mut blob = compress_bytes(b"abc", 1024).unwrap();
    blob.push(0);
    assert_eq!(
        decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap_err(),
        canonical_huffman::Error::TrailingData
    );
}

#[test]
fn length_limit_enforced() {
    let blob = compress_bytes(&vec![1u8; 5000], 1024).unwrap();
    let limits = Limits {
        max_block_bytes: 100,
        max_output_bytes: 1 << 30,
    };
    assert!(matches!(
        decompress_bytes(&blob, &limits, IncompletePolicy::Reject).unwrap_err(),
        canonical_huffman::Error::LimitExceeded { .. }
    ));

    let limits2 = Limits {
        max_block_bytes: 1 << 20,
        max_output_bytes: 100,
    };
    assert!(matches!(
        decompress_bytes(&blob, &limits2, IncompletePolicy::Reject).unwrap_err(),
        canonical_huffman::Error::OutputTooLong { .. }
    ));
}

#[test]
fn multi_block_stream_roundtrip_with_exact_block_boundaries() {
    // 长度恰好是 block_size 的整数倍，考验 EOF 前瞻逻辑。
    for blocks in 1..=4usize {
        for bs in [1usize, 3, 16, 100] {
            let input: Vec<u8> = (0..blocks * bs).map(|i| (i % 5) as u8).collect();
            let blob = compress_bytes(&input, bs).unwrap();
            let out = decompress_bytes(&blob, &default_limits(), IncompletePolicy::Reject).unwrap();
            assert_eq!(out, input, "blocks={blocks} bs={bs}");
        }
    }
}
