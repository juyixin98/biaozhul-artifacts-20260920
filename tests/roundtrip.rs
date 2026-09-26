//! 往返测试：重复字符、周期串、随机数据、文本、边界尺寸。

use lzsw::{compress, decompress};

/// 确定性伪随机（xorshift64*），避免引入 rand 依赖。
struct XorShift(u64);

impl XorShift {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }

    fn bytes(&mut self, n: usize) -> Vec<u8> {
        let mut v = Vec::with_capacity(n);
        while v.len() < n {
            v.extend_from_slice(&self.next().to_le_bytes());
        }
        v.truncate(n);
        v
    }
}

fn roundtrip(data: &[u8], window: usize) -> Vec<u8> {
    let compressed = compress(data, window).expect("compress");
    let back = decompress(&compressed, 1 << 30).expect("decompress");
    assert_eq!(back, data, "roundtrip mismatch (window={window})");
    compressed
}

#[test]
fn repeated_char_compresses_well() {
    let data = vec![b'a'; 100_000];
    let compressed = roundtrip(&data, 4096);
    // 100KB 重复字符应压到远小于 10%
    assert!(
        compressed.len() < data.len() / 10,
        "compressed {} bytes, expected << {}",
        compressed.len(),
        data.len()
    );
}

#[test]
fn periodic_strings() {
    for period in [1usize, 2, 3, 5, 7, 64, 255, 4096, 5000] {
        let mut rng = XorShift(0x9E3779B97F4A7C15 ^ period as u64);
        let pat = rng.bytes(period);
        let mut data = Vec::new();
        while data.len() < 50_000 {
            data.extend_from_slice(&pat);
        }
        data.truncate(50_000);
        roundtrip(&data, 4096);
    }
}

#[test]
fn random_data_roundtrips() {
    let mut rng = XorShift(42);
    let data = rng.bytes(100_000);
    let compressed = roundtrip(&data, 4096);
    // 随机数据不可压，允许少量膨胀（token 开销），但必须可还原
    assert!(compressed.len() < data.len() + data.len() / 64 + 1024);
}

#[test]
fn text_like_data() {
    let words = [
        "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog",
    ];
    let mut rng = XorShift(7);
    let mut data = Vec::new();
    while data.len() < 80_000 {
        let w = words[(rng.next() % words.len() as u64) as usize];
        data.extend_from_slice(w.as_bytes());
        data.push(b' ');
    }
    roundtrip(&data, 4096);
}

#[test]
fn edge_sizes() {
    roundtrip(b"", 4096);
    roundtrip(b"x", 4096);
    roundtrip(b"abc", 4096);
    roundtrip(&[0u8; 129], 4096); // 恰好越过 holdback 边界
    roundtrip(&[0u8; 130], 4096);
    roundtrip(&[0u8; 131], 4096);
    let all_bytes: Vec<u8> = (0u8..=255).cycle().take(10_000).collect();
    roundtrip(&all_bytes, 4096);
}

#[test]
fn various_windows() {
    let mut rng = XorShift(99);
    let mut data = rng.bytes(20_000);
    data.extend_from_slice(&data.clone()); // 制造长程重复
    for window in [16usize, 256, 4096, 65535] {
        roundtrip(&data, window);
    }
}
