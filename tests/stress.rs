//! 大规模随机往返压力测试（默认忽略；用 `cargo test -- --ignored` 运行）。

use canonical_huffman::stream::{compress_bytes, decompress_bytes, IncompletePolicy, Limits};

#[test]
#[ignore = "stress test: 2000 random round trips"]
fn fuzz_2000_random_roundtrips() {
    // xorshift64*，确定性，无需外部 crate。
    let mut state = 0xDEAD_BEEFu64;
    let mut next = || {
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        state.wrapping_mul(0x2545_F491_4F6C_DD1D)
    };

    for trial in 0..2000 {
        let len = (next() % 8192) as usize;
        let mode = next() % 4;
        let alpha: u64 = match mode {
            0 => 1,
            1 => 2 + next() % 6,
            2 => 2 + next() % 255,
            _ => 256,
        };
        let input: Vec<u8> = (0..len).map(|_| (next() % alpha) as u8).collect();
        let block = 1 + (next() % 2048) as usize;

        let blob =
            compress_bytes(&input, block).unwrap_or_else(|e| panic!("trial {trial} compress: {e}"));
        let out = decompress_bytes(&blob, &Limits::default(), IncompletePolicy::Reject)
            .unwrap_or_else(|e| panic!("trial {trial} decompress: {e}"));
        assert_eq!(out, input, "trial {trial} mismatch");
    }
}
