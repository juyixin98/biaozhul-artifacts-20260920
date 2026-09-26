//! Roundtrip tests: repeated characters, periodic strings, random data.

use lzsw::{compress, decompress};

/// Deterministic PRNG (xorshift64*) so tests need no external crates.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545F4914F6CDD1D)
    }

    fn bytes(&mut self, n: usize) -> Vec<u8> {
        (0..n).map(|_| (self.next() >> 32) as u8).collect()
    }
}

fn roundtrip(data: &[u8]) {
    let compressed = compress(data);
    let back = decompress(&compressed, data.len() as u64).expect("decompress");
    assert_eq!(back, data);
}

#[test]
fn empty_input() {
    roundtrip(b"");
}

#[test]
fn repeated_char_compresses_and_roundtrips() {
    let data = vec![b'a'; 100_000];
    let compressed = compress(&data);
    // 100k identical bytes must shrink dramatically (RLE via overlap):
    // ~1 literal + ceil(99999/130) match tokens of 3 bytes each.
    assert!(
        compressed.len() < data.len() / 20,
        "compressed {} bytes into {}",
        data.len(),
        compressed.len()
    );
    let back = decompress(&compressed, data.len() as u64).unwrap();
    assert_eq!(back, data);
}

#[test]
fn periodic_string_roundtrips() {
    let period = b"the quick brown fox jumps over the lazy dog. ";
    let mut data = Vec::new();
    while data.len() < 200_000 {
        data.extend_from_slice(period);
    }
    roundtrip(&data);
}

#[test]
fn short_periods_roundtrip() {
    for p in 1..=16usize {
        let mut data = Vec::new();
        let mut rng = Rng(0x9E3779B97F4A7C15 + p as u64);
        let period = rng.bytes(p);
        while data.len() < 50_000 {
            data.extend_from_slice(&period);
        }
        roundtrip(&data);
    }
}

#[test]
fn random_data_roundtrips() {
    let mut rng = Rng(0xDEADBEEFCAFEF00D);
    let data = rng.bytes(100_000);
    roundtrip(&data);
}

#[test]
fn mixed_content_roundtrips() {
    let mut rng = Rng(42);
    let mut data = Vec::new();
    for i in 0..200 {
        match i % 4 {
            0 => data.extend_from_slice(&vec![b'x'; 500]),
            1 => data.extend_from_slice(b"abcdefghabcdefgh"),
            2 => data.extend_from_slice(&rng.bytes(300)),
            _ => data.extend_from_slice(format!("line {i} of some log output\n").as_bytes()),
        }
    }
    roundtrip(&data);
}

#[test]
fn all_byte_values_roundtrip() {
    let data: Vec<u8> = (0..=255u8).cycle().take(10_000).collect();
    roundtrip(&data);
}
