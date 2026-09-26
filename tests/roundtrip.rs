//! Round-trip tests: empty input, single symbol, equal frequencies,
//! pseudo-random bytes at many sizes, determinism, and skewed data.

use canohuff::{compress_slice, decompress_slice, DecodeOptions};

/// Deterministic xorshift64* PRNG so tests need no external crates.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }

    fn bytes(&mut self, n: usize) -> Vec<u8> {
        let mut out = Vec::with_capacity(n);
        while out.len() < n {
            out.extend_from_slice(&self.next().to_le_bytes());
        }
        out.truncate(n);
        out
    }
}

fn roundtrip(data: &[u8]) {
    let compressed = compress_slice(data).expect("compress");
    let back = decompress_slice(&compressed, &DecodeOptions::default()).expect("decompress");
    assert_eq!(back, data, "round-trip mismatch ({} bytes)", data.len());
}

#[test]
fn empty_input() {
    roundtrip(b"");
}

#[test]
fn single_byte() {
    roundtrip(b"x");
}

#[test]
fn single_symbol_repeated() {
    roundtrip(&vec![0xAB; 1000]);
}

#[test]
fn two_symbols() {
    let mut data = Vec::new();
    for i in 0..10_000 {
        data.push(if i % 3 == 0 { 0 } else { 255 });
    }
    roundtrip(&data);
}

#[test]
fn equal_frequencies_all_symbols() {
    // Every byte value appears exactly 64 times: worst case for Huffman,
    // all code lengths must come out equal (8 bits).
    let mut data = Vec::new();
    for _ in 0..64 {
        for b in 0..=255u8 {
            data.push(b);
        }
    }
    let compressed = compress_slice(&data).expect("compress");
    let back = decompress_slice(&compressed, &DecodeOptions::default()).expect("decompress");
    assert_eq!(back, data);
    // 256 symbols x 8 bits = input size; plus header. Must not panic or
    // corrupt, and expansion is allowed.
}

#[test]
fn random_roundtrip_various_sizes() {
    let mut rng = Rng(0x1234_5678_9ABC_DEF0);
    for size in [1usize, 2, 7, 8, 9, 63, 64, 65, 255, 256, 1000, 4096, 65536, 1 << 20] {
        let data = rng.bytes(size);
        roundtrip(&data);
    }
}

#[test]
fn deterministic_output() {
    let mut rng = Rng(42);
    let data = rng.bytes(5000);
    let a = compress_slice(&data).expect("compress a");
    let b = compress_slice(&data).expect("compress b");
    assert_eq!(a, b, "same input must produce identical output");
}

#[test]
fn skewed_data_compresses_below_input() {
    // Heavily skewed data must shrink even after the header.
    let mut data = Vec::new();
    for i in 0..100_000 {
        data.push(match i % 100 {
            0..=89 => b'e',
            90..=96 => b't',
            _ => b'x',
        });
    }
    let compressed = compress_slice(&data).expect("compress");
    assert!(
        compressed.len() < data.len(),
        "skewed data should compress: {} -> {}",
        data.len(),
        compressed.len()
    );
    let back = decompress_slice(&compressed, &DecodeOptions::default()).expect("decompress");
    assert_eq!(back, data);
}

#[test]
fn random_data_may_expand_but_roundtrips() {
    // Documents the honest contract: incompressible input can grow
    // (header + ~8-bit codes). We only assert a sane bound and round-trip.
    let mut rng = Rng(7);
    let data = rng.bytes(10_000);
    let compressed = compress_slice(&data).expect("compress");
    assert!(
        compressed.len() < data.len() + 1024,
        "expansion should be bounded by header + padding"
    );
    roundtrip(&data);
}
