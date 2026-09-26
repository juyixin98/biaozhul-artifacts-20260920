//! 分块边界测试：编码器/解码器以不同块大小喂入，结果必须一致且可还原。

use lzsw::{Decoder, Encoder, EncoderConfig};

fn test_data() -> Vec<u8> {
    // 混合：重复段 + 周期段 + 类随机段，覆盖匹配与字面量路径
    let mut data = Vec::new();
    data.extend_from_slice(&vec![b'z'; 5000]);
    for i in 0..3000u32 {
        data.extend_from_slice(&(i % 7).to_le_bytes());
    }
    let mut x = 0x12345678u32;
    for _ in 0..8000 {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        data.push((x >> 24) as u8);
    }
    data.extend_from_slice(b"the end. the end. the end.");
    data
}

fn encode_chunked(data: &[u8], chunk: usize) -> Vec<u8> {
    let mut enc = Encoder::new(EncoderConfig::default()).unwrap();
    let mut out = Vec::new();
    for piece in data.chunks(chunk) {
        enc.feed(piece, &mut out).unwrap();
    }
    enc.finish(&mut out).unwrap();
    out
}

fn decode_chunked(data: &[u8], chunk: usize) -> Vec<u8> {
    let mut dec = Decoder::new(1 << 30);
    let mut out = Vec::new();
    for piece in data.chunks(chunk) {
        dec.feed(piece, &mut out).unwrap();
    }
    dec.finish().unwrap();
    out
}

#[test]
fn encoder_chunk_size_does_not_change_output() {
    let data = test_data();
    let reference = encode_chunked(&data, usize::MAX);
    for chunk in [1usize, 2, 3, 7, 128, 129, 1024, 65536] {
        assert_eq!(
            encode_chunked(&data, chunk),
            reference,
            "encoder output differs at chunk={chunk}"
        );
    }
}

#[test]
fn decoder_chunk_size_does_not_matter() {
    let data = test_data();
    let compressed = encode_chunked(&data, 512);
    for chunk in [1usize, 2, 3, 5, 127, 4096, usize::MAX] {
        assert_eq!(
            decode_chunked(&compressed, chunk),
            data,
            "decoder output differs at chunk={chunk}"
        );
    }
}

#[test]
fn cross_chunked_roundtrip() {
    let data = test_data();
    for enc_chunk in [1usize, 100, 4096] {
        let compressed = encode_chunked(&data, enc_chunk);
        for dec_chunk in [1usize, 13, 1024] {
            assert_eq!(decode_chunked(&compressed, dec_chunk), data);
        }
    }
}
