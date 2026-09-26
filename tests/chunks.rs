//! Chunk-boundary tests: streaming the encoder and decoder with awkward
//! chunk sizes must not change the decoded result.

use lzsw::{compress, Decoder, Encoder};

fn sample_data() -> Vec<u8> {
    let mut data = Vec::new();
    // Repeats (match-heavy), some noise, and a tail shorter than MAX_MATCH.
    for i in 0..500u32 {
        data.extend_from_slice(format!("record-{i:04}-payload-payload-payload\n").as_bytes());
        if i % 7 == 0 {
            data.extend_from_slice(&[i as u8; 300]);
        }
    }
    data.extend_from_slice(b"end");
    data
}

fn encode_in_chunks(data: &[u8], chunk: usize) -> Vec<u8> {
    let mut enc = Encoder::new();
    let mut out = Vec::new();
    for c in data.chunks(chunk) {
        out.extend_from_slice(&enc.update(c));
    }
    out.extend_from_slice(&enc.finish());
    out
}

fn decode_in_chunks(data: &[u8], chunk: usize, max_output: u64) -> Vec<u8> {
    let mut dec = Decoder::new(max_output);
    let mut out = Vec::new();
    for c in data.chunks(chunk) {
        out.extend_from_slice(&dec.update(c).expect("decode chunk"));
    }
    dec.finish().expect("finish");
    out
}

#[test]
fn encoder_chunk_size_does_not_affect_decodability() {
    let data = sample_data();
    for chunk in [1, 2, 3, 7, 64, 129, 1024, 65536] {
        let compressed = encode_in_chunks(&data, chunk);
        let back = decode_in_chunks(&compressed, 1 << 20, data.len() as u64);
        assert_eq!(back, data, "encoder chunk size {chunk}");
    }
}

#[test]
fn decoder_chunk_size_does_not_affect_output() {
    let data = sample_data();
    let compressed = compress(&data);
    for chunk in [1, 2, 3, 5, 127, 128, 129, 4096] {
        let back = decode_in_chunks(&compressed, chunk, data.len() as u64);
        assert_eq!(back, data, "decoder chunk size {chunk}");
    }
}

#[test]
fn token_split_across_decoder_chunks() {
    // Hand-build a stream and feed it one byte at a time so every token
    // boundary (tag, distance bytes, literal payload) is split.
    let mut s = Vec::new();
    s.extend_from_slice(b"LZSW");
    s.extend_from_slice(&[1, 0, 15, 0]);
    s.push(2); // 3 literals
    s.extend_from_slice(b"abc");
    s.push(0x80 | 3); // match len 6
    s.extend_from_slice(&3u16.to_be_bytes()); // distance 3 -> "abcabc"
    let back = decode_in_chunks(&s, 1, 1 << 20);
    assert_eq!(back, b"abcabcabc");
}

#[test]
fn large_stream_crosses_window_compaction() {
    // Force several encoder buffer compactions (> 2*WINDOW consumed) and
    // decoder history trims, then verify integrity.
    let mut data = Vec::new();
    let mut x: u64 = 0x123456789ABCDEF;
    while data.len() < 300_000 {
        x ^= x << 13;
        x ^= x >> 7;
        // Semi-compressible: repeated words with varying suffixes.
        data.extend_from_slice(b"header-block ");
        data.extend_from_slice((x % 1000).to_string().as_bytes());
        data.push(b'\n');
    }
    let compressed = encode_in_chunks(&data, 8191); // odd chunk size on purpose
    assert!(compressed.len() < data.len() / 2);
    let back = decode_in_chunks(&compressed, 1021, data.len() as u64);
    assert_eq!(back, data);
}

#[test]
fn empty_input_through_streaming_api() {
    let mut enc = Encoder::new();
    let tail = enc.finish();
    let mut dec = Decoder::new(16);
    let out = dec.update(&tail).unwrap();
    dec.finish().unwrap();
    assert!(out.is_empty());
}
